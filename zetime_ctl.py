"""
zetime_ctl.py -- client Bluetooth LE minimaliste pour la montre MyKronoz ZeTime,
utilisable sous Windows sans l'application officielle myKronoz.

CONTEXTE
--------
L'application officielle myKronoz est fermee (pas de code source public, pas de
SDK). Ce script reimplemente en Python le protocole BLE proprietaire de la ZeTime
tel que documente publiquement par le projet communautaire Gadgetbridge (AGPLv3),
a partir de son analyse de protocole redigee par Sauce Maison et Sebastian Kranz :
    https://gadgetbridge.org/internals/specifics/zetime-protocol/

Ceci est un code Python original, ecrit a partir de cette documentation de
protocole (formats de trame, "handles" BLE, opcodes) -- ce n'est pas une
traduction du code source Java de Gadgetbridge.

IMPORTANT -- A LIRE AVANT UTILISATION
--------------------------------------
- Les commandes de LECTURE (scan, discover, info) ont ete validees sur une
  ZeTime reelle (firmware de 2017) : la montre repond bien au protocole decrit
  ici. Cote ECRITURE, "synctime" a ete validee (l'heure affichee change) ;
  "notify" et "weather" restent non validees sur materiel.
- "synctime" ne recale PAS les aiguilles : leur position est suivie
  mecaniquement par la montre et peut deriver de son horloge interne. Les
  remettre d'aplomb demande la procedure de calibration (rotation des aiguilles
  jusqu'au repere de midi), exposee par l'app officielle mais absente de la
  documentation publique du protocole -- donc non implementable ici.
- Une divergence de firmware entre unites peut exister. Utilisez d'abord la
  commande "discover" (voir plus bas) pour verifier les caracteristiques BLE
  de VOTRE montre avant d'utiliser les autres commandes, et testez d'abord des
  commandes sans risque (info, discover) avant d'envoyer des notifications ou
  d'ecrire l'heure.
- Fonctions volontairement NON implementees car non documentees de façon fiable
  dans la source publique (mieux vaut ne rien envoyer que d'envoyer un octet
  invente au hasard sur un peripherique reel) : niveau de batterie, profil
  utilisateur (taille/poids/age), objectifs (pas/calories), mise a jour du
  firmware (non supportee par personne, y compris Gadgetbridge lui-meme).
- La meteo multi-jours est simplifiee : la meme prevision est envoyee pour les
  4 blocs (jour courant + 3 jours), faute de detail public sur la structure
  exacte des jours J+1/J+2/J+3. A ajuster si besoin une fois verifie sur le
  materiel reel.

PREREQUIS (Windows 10/11, Python 3.9+)
---------------------------------------
    pip install bleak

ADRESSE DE LA MONTRE
--------------------
Copiez zetime.local.example en zetime.local et renseignez-y l'adresse relevee
par "scan" :

    address = AA:BB:CC:DD:EE:FF

Toutes les commandes l'utiliseront alors par defaut, et --address ne sert plus
qu'a viser ponctuellement une autre montre. zetime.local est ignore par git :
l'adresse MAC, qui identifie un appareil physique precis, ne part pas dans un
depot public.

UTILISATION
-----------
    python zetime_ctl.py scan
        Liste les peripheriques BLE a proximite (repere l'adresse MAC de la montre).

    python zetime_ctl.py discover
        Affiche tous les services/caracteristiques BLE exposes par la montre,
        avec leurs "handles" -- a comparer aux constantes UUID_* ci-dessous.

    python zetime_ctl.py info
    python zetime_ctl.py synctime
    python zetime_ctl.py activity
    python zetime_ctl.py sleep
    python zetime_ctl.py heartrate
    python zetime_ctl.py notify  --type sms --title "Maman" --body "Bien arrive ?"
    python zetime_ctl.py weather --now 18 --min 12 --max 21 --icon cloudy --city "Geneve"

    python zetime_ctl.py info --address AA:BB:CC:DD:EE:FF
        Vise une autre montre que celle de zetime.local.
"""

import argparse
import asyncio
import struct
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner

# ---------------------------------------------------------------------------
# Constantes de protocole (source : documentation Gadgetbridge, voir en-tete)
# ---------------------------------------------------------------------------

PREAMBLE = 0x6F
END = 0x8F
CHUNK = 20  # taille max d'une ecriture GATT dans ce protocole

# Caracteristiques BLE de la ZeTime, service 00006006-0000-1000-8000-00805f9b34fb.
#
# On les adresse par UUID et non par "handle" numerique : la documentation
# Gadgetbridge cite les handles de VALEUR (0x0012 / 0x0016 / 0x001a / 0x001e),
# alors que bleak sous Windows expose les handles de DECLARATION, soit un de
# moins (0x0011 / 0x0015 / 0x0019 / 0x001d). Passer 0x0012 a bleak echouerait
# donc a resoudre la caracteristique. L'UUID, lui, est non ambigu.
# Verifie sur une ZeTime reelle avec la commande "discover".
UUID_WRITE = "00008001-0000-1000-8000-00805f9b34fb"   # telephone -> montre : corps du message (handle 0x0012)
UUID_ACK = "00008002-0000-1000-8000-00805f9b34fb"     # telephone -> montre : ecrire 0x03 pour valider ; montre -> telephone : reponses en notification (handle 0x0016)
UUID_REPLY = "00008003-0000-1000-8000-00805f9b34fb"   # telephone -> montre : reponses aux requetes initiees par la montre (handle 0x001a)
UUID_NOTIFY = "00008004-0000-1000-8000-00805f9b34fb"  # montre -> telephone : requetes initiees par la montre, en notification (handle 0x001e)

# Les quatre caracteristiques n'exposent que "write-without-response" (pas de
# "write") : toute ecriture doit donc etre non acquittee au niveau ATT.
WRITE_RESPONSE = False

NOTIF_TYPES = {
    "missed_call": 0x00, "sms": 0x01, "email": 0x03, "call": 0x05,
    "call_end": 0x06, "viber": 0x08, "snapchat": 0x09, "whatsapp": 0x0A,
    "facebook": 0x0C, "hangout": 0x0D, "gmail": 0x0E, "messenger": 0x0F,
    "instagram": 0x10,
}

WEATHER_ICONS = {"cloudy": 0x01, "rainy": 0x04, "stormy": 0x06}


def build_frame(subject: int, type_: int, payload: bytes = b"") -> bytes:
    """Construit une trame : 6f + subject + type + longueur(2, LE) + payload + 8f."""
    return bytes([PREAMBLE, subject, type_]) + struct.pack("<H", len(payload)) + payload + bytes([END])


def parse_frame(frame: Optional[bytes]):
    """Inverse de build_frame : retourne (subject, type, payload) ou None si invalide."""
    if not frame or len(frame) < 6 or frame[0] != PREAMBLE or frame[-1] != END:
        return None
    length = struct.unpack("<H", frame[3:5])[0]
    return frame[1], frame[2], frame[5:5 + length]


def chunk_frame(frame: bytes):
    for i in range(0, len(frame), CHUNK):
        yield frame[i:i + CHUNK]


def build_time_payload(when: Optional[datetime] = None) -> bytes:
    """Payload de la trame "reglage de l'heure" (subject 0x04, type 0x71).

    Les 5 octets qui suivent les secondes sont presentes comme "non expliques"
    par la page de protocole de Gadgetbridge, qui conseille la valeur fixe
    0000010100. Le code source de Gadgetbridge, lui, les nomme :
        [0] format 24h, [1] "SetTime after calibration", [2] unite,
        [3] decalage horaire en HEURES, [4] decalage horaire en MINUTES.
    Le 4e octet de cette constante figee vaut 0x01, soit UTC+1 code en dur :
    l'envoyer tel quel decale la montre d'une heure des qu'on n'est pas a UTC+1
    -- heure d'ete europeenne comprise. On le calcule donc a chaque appel depuis
    le fuseau du systeme.
    """
    when = when or datetime.now()
    if when.tzinfo is None:
        when = when.astimezone()  # attache le fuseau local, sans decaler l'heure lue

    # Le signe est porte par l'octet des heures, les minutes restent en valeur
    # absolue. Les fuseaux a 30/45 min sont une deduction : aucune ZeTime n'a
    # ete testee ailleurs qu'a un decalage entier.
    total_minutes = int((when.utcoffset() or timedelta(0)).total_seconds()) // 60
    tz_hour = int(total_minutes / 60)   # tronque vers zero, comme Gadgetbridge
    tz_minute = abs(total_minutes) % 60

    return (
        struct.pack("<H", when.year)
        + bytes([when.month, when.day, when.hour, when.minute, when.second])
        + bytes([0x00, 0x00, 0x01, tz_hour & 0xFF, tz_minute])
    )


class ZeTime:
    """Petite couche au-dessus de bleak pour parler le protocole ZeTime."""

    def __init__(self, client: BleakClient):
        self.client = client
        # Deux flux distincts, volontairement NON melanges :
        #  - UUID_ACK (8002) porte les reponses de la montre a NOS commandes ;
        #  - UUID_NOTIFY (8004) porte les requetes que la montre initie seule
        #    (controle musique, "retrouver mon telephone", rejet d'appel...).
        # Les confondre ferait consommer une requete spontanee a la place de la
        # reponse attendue si la montre parle pendant qu'on l'interroge.
        self._responses: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._watch_requests: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._buffers = {"ack": bytearray(), "watch": bytearray()}

    def _feed(self, stream: str, data: bytes, queue: "asyncio.Queue[bytes]"):
        """Reassemble les trames completes d'un flux.

        Une notification ne vaut pas une trame : au-dela de 20 octets la montre
        decoupe sa reponse en plusieurs notifications. On accumule donc par flux
        et on ne publie qu'une trame entiere, dont la longueur est donnee par
        l'en-tete (6f + subject + type + longueur sur 2 octets + payload + 8f).
        """
        buf = self._buffers[stream]
        buf += data
        while True:
            start = buf.find(PREAMBLE)
            if start == -1:            # rien d'exploitable dans le tampon
                buf.clear()
                return
            del buf[:start]            # on jette les octets avant le preambule
            if len(buf) < 5:
                return                 # en-tete incomplet : on attend la suite
            total = 5 + struct.unpack("<H", buf[3:5])[0] + 1
            if total > 4096:           # longueur aberrante : faux preambule
                del buf[:1]
                continue
            if len(buf) < total:
                return                 # trame incomplete : on attend la suite
            if buf[total - 1] == END:
                queue.put_nowait(bytes(buf[:total]))
                del buf[:total]
            else:
                del buf[:1]            # terminateur absent : on resynchronise

    def _on_ack(self, _sender, data: bytearray):
        self._feed("ack", bytes(data), self._responses)

    def _on_watch(self, _sender, data: bytearray):
        self._feed("watch", bytes(data), self._watch_requests)

    async def start(self):
        await self.client.start_notify(UUID_ACK, self._on_ack)
        await self.client.start_notify(UUID_NOTIFY, self._on_watch)

    async def next_watch_request(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Prochaine requete initiee par la montre (flux 8004), ou None si delai depasse.

        Y repondre suppose d'ecrire sur UUID_REPLY (8003) : non implemente, faute
        de description fiable des formats de reponse dans la source publique.
        """
        if timeout is None:
            return await self._watch_requests.get()
        try:
            return await asyncio.wait_for(self._watch_requests.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _await_response(self, subject: int, timeout: float) -> Optional[bytes]:
        """Attend une reponse portant le subject demande.

        Une trame au mauvais subject (reponse tardive d'une commande precedente)
        est signalee et ignoree, sans consommer le delai restant d'un coup.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                print("  (pas de reponse recue avant expiration du delai)")
                return None
            try:
                frame = await asyncio.wait_for(self._responses.get(), timeout=remaining)
            except asyncio.TimeoutError:
                print("  (pas de reponse recue avant expiration du delai)")
                return None
            parsed = parse_frame(frame)
            if parsed and parsed[0] != subject:
                print(f"  (trame ignoree : subject 0x{parsed[0]:02x}, attendu 0x{subject:02x})")
                continue
            return frame

    async def send(self, subject: int, type_: int, payload: bytes = b"",
                    expect_reply: bool = True, timeout: float = 5.0) -> Optional[bytes]:
        frame = build_frame(subject, type_, payload)
        for part in chunk_frame(frame):
            await self.client.write_gatt_char(UUID_WRITE, part, response=WRITE_RESPONSE)
            # Sans acquittement ATT, rien ne regule le debit : une courte pause
            # evite que la montre perde des morceaux d'une trame longue.
            await asyncio.sleep(0.02)
        # Ecriture de validation (documentee : "0x03" sur la caracteristique ACK)
        await self.client.write_gatt_char(UUID_ACK, bytes([0x03]), response=WRITE_RESPONSE)
        if not expect_reply:
            return None
        return await self._await_response(subject, timeout)

    # ---- commandes documentees ----

    async def get_serial(self):
        return await self.send(0x02, 0x70, b"\x00")

    async def get_info_availability(self):
        return await self.send(0x52, 0x70, b"\x00")

    async def sync_time(self, when: Optional[datetime] = None):
        return await self.send(0x04, 0x71, build_time_payload(when), expect_reply=False)

    async def get_activity(self, packet: int = 0):
        return await self.send(0x54, 0x70, struct.pack("<H", packet))

    async def get_sleep(self, packet: int = 0):
        return await self.send(0x56, 0x70, struct.pack("<H", packet))

    async def get_heartrate(self, packet: int = 0):
        return await self.send(0x61, 0x70, struct.pack("<H", packet))

    async def push_notification(self, kind: str, title: str, body: str, count: int = 1):
        type_byte = NOTIF_TYPES[kind]
        title_b = title.encode("utf-8")[:255]
        body_b = body.encode("utf-8")[:255]
        ts = datetime.now().strftime("%Y%m%dT%H%M%S").encode("ascii")
        payload = bytes([type_byte, count, len(title_b), len(body_b)]) + title_b + body_b + ts
        return await self.send(0x76, 0x71, payload, expect_reply=False)

    async def push_weather(self, now_c: int, min_c: int, max_c: int, icon: str, city: str):
        icon_b = WEATHER_ICONS.get(icon, 0x01)

        def day_block(cur, mn, mx):
            return bytes([0x00, cur & 0xFF, mn & 0xFF, mx & 0xFF, icon_b])

        # simplification : meme prevision repetee sur les 4 blocs (voir avertissement en-tete)
        payload = day_block(now_c, min_c, max_c) * 4 + city.encode("utf-8")
        return await self.send(0x77, 0x71, payload, expect_reply=False)


# ---------------------------------------------------------------------------
# Commandes CLI
# ---------------------------------------------------------------------------

def looks_like_zetime(name: Optional[str], adv) -> bool:
    """Heuristique : la ZeTime s'annonce sous un nom contenant 'zetime' ou 'kronoz'."""
    for candidate in (name, getattr(adv, "local_name", None)):
        if candidate and any(k in candidate.lower() for k in ("zetime", "kronoz")):
            return True
    return False


async def cmd_scan(timeout: float = 20.0):
    """Scan BLE detaille : nom, puissance du signal, UUID de services, donnees fabricant.

    Le nom d'un peripherique BLE arrive souvent dans la "scan response" et non dans
    l'annonce initiale : on ecoute donc en continu et on fusionne les informations
    recues au fil du temps, au lieu de ne regarder qu'un seul instantane.
    """
    print(f"Recherche de peripheriques Bluetooth LE ({timeout:.0f}s)...")
    print("Laissez la montre reveillee et a moins d'un metre pendant tout le scan.\n")

    seen: dict = {}

    def on_detect(device, adv):
        key = device.address
        entry = seen.setdefault(key, {"name": None, "rssi": None, "uuids": set(), "mfr": {}, "hits": 0})
        entry["hits"] += 1
        entry["name"] = adv.local_name or device.name or entry["name"]
        entry["rssi"] = adv.rssi
        entry["uuids"].update(adv.service_uuids or [])
        entry["mfr"].update(adv.manufacturer_data or {})
        if looks_like_zetime(entry["name"], adv) and not entry.get("announced"):
            entry["announced"] = True
            print(f"  >>> ZeTime detectee : {key}  ({entry['name']})")

    scanner = BleakScanner(detection_callback=on_detect, scanning_mode="active")
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    if not seen:
        print("Aucun peripherique trouve. Verifiez que le Bluetooth est active et que la montre est a proximite.")
        return

    print(f"\n{len(seen)} peripherique(s) vu(s), du plus proche au plus lointain :\n")
    for address, e in sorted(seen.items(), key=lambda kv: kv[1]["rssi"] or -999, reverse=True):
        name = e["name"] or "(sans nom diffuse)"
        marker = "   <-- probablement la ZeTime" if looks_like_zetime(e["name"], None) else ""
        print(f"{address}   {e['rssi']:>4} dBm   {name}{marker}")
        if e["uuids"]:
            print(f"                       services : {', '.join(sorted(e['uuids']))}")
        for cid, data in e["mfr"].items():
            print(f"                       fabricant 0x{cid:04x} : {data.hex()}")

    if not any(looks_like_zetime(e["name"], None) for e in seen.values()):
        print("\nAucun nom evoquant 'ZeTime'/'Kronoz'. Pistes, dans l'ordre :")
        print("  1. La montre est-elle deja connectee a votre telephone ? Une ZeTime connectee")
        print("     n'emet plus d'annonce BLE : coupez le Bluetooth du telephone (ou 'oublier'")
        print("     la montre dans l'app myKronoz), puis relancez ce scan.")
        print("  2. Reveillez l'ecran de la montre juste avant / pendant le scan.")
        print("  3. Certaines ZeTime n'annoncent leur nom qu'en mode appairage : cherchez")
        print("     Reglages > Bluetooth (ou 'Appairage'/'Connexion') dans les menus de la montre.")
        print("  4. Un peripherique sans nom mais avec un signal fort (> -60 dBm) qui apparait")
        print("     seulement quand la montre est reveillee est un bon candidat :")
        print("     testez-le avec 'python zetime_ctl.py discover --address <MAC>'.")


async def cmd_discover(address: str):
    async with BleakClient(address) as client:
        print(f"Connecte a {address}. Services et caracteristiques exposes :\n")
        for service in client.services:
            print(f"[Service] {service.uuid}")
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                print(f"    handle=0x{ch.handle:04x}  uuid={ch.uuid}  proprietes=({props})")
        print("\nComparez ces UUID a UUID_WRITE/UUID_ACK/UUID_REPLY/UUID_NOTIFY en haut du")
        print("script (8001 / 8002 / 8003 / 8004 dans le service 6006). Note : bleak affiche")
        print("ici le handle de DECLARATION ; le handle de VALEUR cite par Gadgetbridge")
        print("(0x0012 / 0x0016 / 0x001a / 0x001e) vaut celui-ci + 1.")


async def cmd_info(address: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()

        serial = parse_frame(await zt.get_serial())
        if serial:
            print("Numero de serie :", serial[2].decode("ascii", "replace"))
        else:
            print("Numero de serie : reponse illisible")

        avail = parse_frame(await zt.get_info_availability())
        if avail and len(avail[2]) >= 8:
            # Deux compteurs 32 bits little-endian. La doc publique ne nomme pas
            # explicitement les deux champs : on les affiche sans sur-interpreter.
            a, b = struct.unpack("<II", avail[2][:8])
            print(f"Donnees en attente sur la montre : compteur 1 = {a}, compteur 2 = {b}")
        else:
            print("Disponibilite des donnees : reponse illisible")


async def cmd_synctime(address: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        await zt.sync_time()
        print("Heure envoyee a la montre.")


async def cmd_notify(address: str, kind: str, title: str, body: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        await zt.push_notification(kind, title, body)
        print("Notification envoyee.")


async def cmd_activity(address: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        print("Activite (brut) :", await zt.get_activity())


async def cmd_sleep(address: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        print("Sommeil (brut) :", await zt.get_sleep())


async def cmd_heartrate(address: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        print("Frequence cardiaque (brut) :", await zt.get_heartrate())


async def cmd_weather(address: str, now: int, mn: int, mx: int, icon: str, city: str):
    async with BleakClient(address) as client:
        zt = ZeTime(client)
        await zt.start()
        await zt.push_weather(now, mn, mx, icon, city)
        print("Meteo envoyee.")


# ---------------------------------------------------------------------------
# Configuration locale
# ---------------------------------------------------------------------------

# L'adresse MAC d'une montre identifie un appareil physique precis : elle n'a rien
# a faire dans un depot public. Elle est donc lue depuis un fichier local, ignore
# par git (voir .gitignore), et jamais ecrite en dur dans le code ou le README.
# Le fichier est cherche a cote du script, pas dans le repertoire courant, pour
# que les commandes marchent depuis n'importe ou.
LOCAL_CONFIG = Path(__file__).with_name("zetime.local")


def load_local_config() -> dict:
    """Lit zetime.local : lignes 'cle = valeur', '#' en commentaire. Absent = {}."""
    if not LOCAL_CONFIG.exists():
        return {}
    config = {}
    for line in LOCAL_CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            config[key.strip().lower()] = value.strip()
    return config


def resolve_address(explicit: Optional[str]) -> str:
    """Adresse de la montre : --address s'il est donne, sinon zetime.local."""
    if explicit:
        return explicit
    address = load_local_config().get("address")
    if address:
        return address
    raise SystemExit(
        f"Aucune adresse de montre.\n"
        f"  - soit passez --address AA:BB:CC:DD:EE:FF (voir la commande 'scan'),\n"
        f"  - soit creez {LOCAL_CONFIG.name} a cote du script avec une ligne :\n"
        f"        address = AA:BB:CC:DD:EE:FF\n"
        f"    (voir zetime.local.example ; ce fichier n'est pas versionne)"
    )


def main():
    p = argparse.ArgumentParser(
        description="Client BLE minimaliste pour MyKronoz ZeTime (protocole communautaire Gadgetbridge, non officiel)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan")
    sp.add_argument("--timeout", type=float, default=20.0, help="duree du scan en secondes (defaut : 20)")

    # --address est facultatif : a defaut, il est lu dans zetime.local.
    addr_help = "Adresse MAC BLE de la montre (defaut : celle de zetime.local ; voir 'scan')"

    for name in ("discover", "info", "synctime", "activity", "sleep", "heartrate"):
        sp = sub.add_parser(name)
        sp.add_argument("--address", help=addr_help)

    sp = sub.add_parser("notify")
    sp.add_argument("--address", help=addr_help)
    sp.add_argument("--type", required=True, choices=sorted(NOTIF_TYPES))
    sp.add_argument("--title", default="")
    sp.add_argument("--body", default="")

    sp = sub.add_parser("weather")
    sp.add_argument("--address", help=addr_help)
    sp.add_argument("--now", type=int, required=True)
    sp.add_argument("--min", type=int, required=True, dest="mn")
    sp.add_argument("--max", type=int, required=True, dest="mx")
    sp.add_argument("--icon", default="cloudy", choices=sorted(WEATHER_ICONS))
    sp.add_argument("--city", default="")

    args = p.parse_args()

    # "scan" est la seule commande qui n'a pas besoin d'une adresse : elle sert
    # justement a la trouver. Sa lambda ignore addr, d'ou la chaine vide.
    addr = "" if args.cmd == "scan" else resolve_address(args.address)

    coroutines = {
        "scan": lambda: cmd_scan(args.timeout),
        "discover": lambda: cmd_discover(addr),
        "info": lambda: cmd_info(addr),
        "synctime": lambda: cmd_synctime(addr),
        "notify": lambda: cmd_notify(addr, args.type, args.title, args.body),
        "activity": lambda: cmd_activity(addr),
        "sleep": lambda: cmd_sleep(addr),
        "heartrate": lambda: cmd_heartrate(addr),
        "weather": lambda: cmd_weather(addr, args.now, args.mn, args.mx, args.icon, args.city),
    }
    asyncio.run(coroutines[args.cmd]())


if __name__ == "__main__":
    main()
