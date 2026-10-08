"""
zetime_gui.py -- interface graphique pour zetime_ctl.py (MyKronoz ZeTime).

Cherche les montres a proximite, en selectionne une, puis lance les commandes
du protocole dessus. Toute la logique BLE vient de zetime_ctl : ce fichier ne
reimplemente aucune trame, il ne fait qu'habiller le module existant.

    python zetime_gui.py

PREREQUIS
---------
bleak (deja requis par zetime_ctl) et Tkinter, livre avec Python sous Windows.
Aucune dependance supplementaire : c'est le critere qui a fait choisir Tkinter
plutot que Qt, pour un projet qui n'a jusqu'ici que bleak comme dependance.

DEUX BOUCLES D'EVENEMENTS
-------------------------
bleak est asyncio, Tkinter a sa propre boucle bloquante : les deux ne peuvent
pas tourner sur le meme thread. La boucle asyncio vit donc dans un thread demon
(AsyncRunner), et les resultats reviennent a l'interface via root.after() --
seul endroit d'ou l'on a le droit de toucher aux widgets. Aucun appel Tkinter
n'est fait depuis le thread asyncio, ni l'inverse.

CONVENTION D'ECRITURE
---------------------
Les libelles affiches sont accentues (Tkinter gere l'Unicode sans probleme),
les commentaires restent en ASCII comme dans zetime_ctl.py, dont les messages
partent eux vers une console Windows.
"""

import asyncio
import queue
import struct
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

from zetime_ctl import (
    NOTIF_TYPES,
    WEATHER_ICONS,
    ZeTime,
    build_time_payload,
    load_local_config,
    looks_like_zetime,
    parse_frame,
)

# Les commandes d'ecriture non encore confrontees a une montre reelle. La
# distinction vient de l'en-tete de zetime_ctl : on la fait voir plutot que de
# laisser croire que tous les boutons se valent.
AVERTISSEMENT_NON_TESTE = (
    "⚠ Charge utile jamais validée sur matériel réel. La plomberie BLE, elle, "
    "est celle qui fonctionne pour les autres commandes."
)


# ---------------------------------------------------------------------------
# Pont entre asyncio et Tkinter
# ---------------------------------------------------------------------------

class AsyncRunner:
    """Boucle asyncio dans un thread demon, resultats rendus au thread Tkinter."""

    POLL_MS = 40

    def __init__(self, root: tk.Tk):
        self.root = root
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, on_ok: Callable, on_error: Callable):
        """Lance une coroutine ; on_ok/on_error sont appeles cote Tkinter."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def verifier():
            if not future.done():
                self.root.after(self.POLL_MS, verifier)
                return
            try:
                resultat = future.result()
            except Exception as exc:  # une montre absente ou endormie lève ici
                on_error(exc)
            else:
                on_ok(resultat)

        self.root.after(self.POLL_MS, verifier)

    def run_blocking(self, coro, timeout: float = 3.0):
        """Attend un resultat en bloquant : reserve a la fermeture de l'appli."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        except Exception:
            return None

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------------------
# Acces a la montre
# ---------------------------------------------------------------------------

class Session:
    """Execute des commandes sur une montre, dans l'un des deux modes possibles.

    - mode ponctuel (defaut) : chaque commande ouvre puis referme la connexion,
      exactement comme le CLI. Plus lent (2 a 5 s par commande) mais insensible
      aux deconnexions entre deux commandes.
    - mode maintenu : la connexion reste ouverte, les commandes s'enchainent
      sans attente. Si la montre s'est eloignee entre-temps, on se reconnecte
      de facon transparente a la commande suivante.
    """

    def __init__(self, address: str, persistent: bool):
        self.address = address
        self.persistent = persistent
        self._client: Optional[BleakClient] = None
        self._zt: Optional[ZeTime] = None

    async def run(self, action: Callable):
        """Appelle action(zt) en gerant la connexion selon le mode choisi."""
        if not self.persistent:
            async with BleakClient(self.address) as client:
                zt = ZeTime(client)
                await zt.start()
                return await action(zt)

        if self._client is None or not self._client.is_connected:
            await self.close()
            client = BleakClient(self.address)
            await client.connect()
            zt = ZeTime(client)
            await zt.start()
            self._client, self._zt = client, zt
        return await action(self._zt)

    async def connect(self):
        """Ouvre la connexion tout de suite, pour que l'utilisateur voie l'etat."""
        if self.persistent:
            await self.run(lambda zt: asyncio.sleep(0))

    async def close(self):
        client, self._client, self._zt = self._client, None, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass  # deja tombee : rien de plus a faire que d'oublier le client

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def hexdump(data: bytes, largeur: int = 16) -> str:
    """Trame brute en offset + hexa + ASCII, format classique de retro-ingenierie."""
    lignes = []
    for offset in range(0, len(data), largeur):
        tranche = data[offset:offset + largeur]
        hexa = " ".join(f"{b:02x}" for b in tranche).ljust(largeur * 3 - 1)
        texte = "".join(chr(b) if 32 <= b < 127 else "." for b in tranche)
        lignes.append(f"{offset:04x}  {hexa}  {texte}")
    return "\n".join(lignes)


def decalage_lisible(payload: bytes) -> str:
    """Relit le fuseau depuis le payload d'heure, pour l'afficher avant envoi."""
    heures = payload[10] - 256 if payload[10] > 127 else payload[10]
    minutes = payload[11]
    return f"UTC{heures:+d}" + (f":{minutes:02d}" if minutes else "")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.runner = AsyncRunner(root)
        self.session: Optional[Session] = None
        self.scan_queue: "queue.Queue[dict]" = queue.Queue()
        self.adresse_enregistree: Optional[str] = None
        self.appareils: dict = {}
        self.dernier_brut: Optional[bytes] = None
        self.dernier_brut_nom = ""
        self.boutons_action: list = []
        self.occupe = False

        root.title("ZeTime Connect")
        root.geometry("980x680")
        root.minsize(860, 600)
        root.protocol("WM_DELETE_WINDOW", self._fermer)

        self._construire()
        self._charger_adresse_enregistree()
        self._tic_horloge()

    # ---- construction de l'interface ----

    def _construire(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        barre = ttk.Frame(self.root, padding=(10, 8))
        barre.grid(row=0, column=0, sticky="ew")
        self.etat = ttk.Label(barre, text="Aucune montre sélectionnée", foreground="#888")
        self.etat.pack(side="right")

        corps = ttk.Panedwindow(self.root, orient="horizontal")
        corps.grid(row=1, column=0, sticky="nsew", padx=10)
        corps.add(self._panneau_montres(corps), weight=1)
        corps.add(self._panneau_commandes(corps), weight=2)

        journal = ttk.LabelFrame(self.root, text="Journal", padding=6)
        journal.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 10))
        journal.columnconfigure(0, weight=1)
        self.journal = tk.Text(journal, height=6, wrap="word", state="disabled",
                               font=("Consolas", 9))
        self.journal.grid(row=0, column=0, sticky="ew")
        barre_j = ttk.Scrollbar(journal, command=self.journal.yview)
        barre_j.grid(row=0, column=1, sticky="ns")
        self.journal.configure(yscrollcommand=barre_j.set)

    def _panneau_montres(self, parent) -> ttk.Frame:
        cadre = ttk.Frame(parent, padding=(0, 0, 8, 0))
        cadre.rowconfigure(1, weight=1)
        cadre.columnconfigure(0, weight=1)

        haut = ttk.Frame(cadre)
        haut.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ligne1 = ttk.Frame(haut)
        ligne1.pack(fill="x")
        self.bouton_scan = ttk.Button(ligne1, text="Rechercher les montres",
                                      command=self._scanner)
        self.bouton_scan.pack(side="left")
        ttk.Label(ligne1, text="durée").pack(side="left", padx=(8, 3))
        self.duree_scan = tk.IntVar(value=20)
        ttk.Spinbox(ligne1, from_=5, to=60, width=4,
                    textvariable=self.duree_scan).pack(side="left")
        ttk.Label(ligne1, text="s").pack(side="left", padx=(2, 0))
        self.afficher_tout = tk.BooleanVar(value=False)
        ttk.Checkbutton(haut, text="Afficher tous les appareils BLE",
                        variable=self.afficher_tout,
                        command=self._rafraichir_liste).pack(anchor="w", pady=(4, 0))

        colonnes = ("adresse", "nom", "signal")
        self.liste = ttk.Treeview(cadre, columns=colonnes, show="headings",
                                  selectmode="browse")
        for col, titre, largeur in (("adresse", "Adresse", 150),
                                    ("nom", "Nom", 130),
                                    ("signal", "Signal", 65)):
            self.liste.heading(col, text=titre)
            self.liste.column(col, width=largeur,
                              anchor="e" if col == "signal" else "w")
        self.liste.grid(row=1, column=0, sticky="nsew")
        self.liste.tag_configure("zetime", foreground="#0a6d2e")
        self.liste.bind("<Double-1>", lambda _e: self._selectionner())
        defil = ttk.Scrollbar(cadre, command=self.liste.yview)
        defil.grid(row=1, column=1, sticky="ns")
        self.liste.configure(yscrollcommand=defil.set)

        bas = ttk.LabelFrame(cadre, text="Connexion", padding=6)
        bas.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.mode_maintenu = tk.BooleanVar(value=False)
        ttk.Radiobutton(bas, text="Une connexion par commande",
                        variable=self.mode_maintenu, value=False).pack(anchor="w")
        ttk.Radiobutton(bas, text="Garder la connexion ouverte",
                        variable=self.mode_maintenu, value=True).pack(anchor="w")
        actions = ttk.Frame(bas)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="Sélectionner",
                   command=self._selectionner).pack(side="left")
        self.bouton_deconnecter = ttk.Button(actions, text="Déconnecter",
                                             command=self._deconnecter, state="disabled")
        self.bouton_deconnecter.pack(side="left", padx=(6, 0))
        return cadre

    def _panneau_commandes(self, parent) -> ttk.Frame:
        cadre = ttk.Frame(parent)
        cadre.rowconfigure(0, weight=1)
        cadre.columnconfigure(0, weight=1)
        self.onglets = ttk.Notebook(cadre)
        self.onglets.grid(row=0, column=0, sticky="nsew")
        self.onglets.add(self._onglet_info(), text="Informations")
        self.onglets.add(self._onglet_heure(), text="Heure")
        self.onglets.add(self._onglet_historiques(), text="Historiques")
        self.onglets.add(self._onglet_notification(), text="Notification")
        self.onglets.add(self._onglet_meteo(), text="Météo")
        return cadre

    def _bouton_action(self, parent, texte, commande) -> ttk.Button:
        """Bouton desactive tant qu'aucune montre n'est choisie ou qu'on est occupe."""
        bouton = ttk.Button(parent, text=texte, command=commande, state="disabled")
        self.boutons_action.append(bouton)
        return bouton

    def _onglet_info(self) -> ttk.Frame:
        f = ttk.Frame(self.onglets, padding=12)
        self._bouton_action(f, "Lire les informations", self._lire_info).pack(anchor="w")
        self.info_serie = ttk.Label(f, text="Numéro de série : —")
        self.info_serie.pack(anchor="w", pady=(14, 2))
        self.info_donnees = ttk.Label(f, text="Données en attente : —")
        self.info_donnees.pack(anchor="w")
        ttk.Label(f, wraplength=520, foreground="#666", text=(
            "Les deux compteurs de « données en attente » ne sont pas nommés par la "
            "documentation publique du protocole : ils sont affichés tels quels, sans "
            "interprétation."
        )).pack(anchor="w", pady=(14, 0))
        return f

    def _onglet_heure(self) -> ttk.Frame:
        f = ttk.Frame(self.onglets, padding=12)
        self.heure_pc = ttk.Label(f, font=("Segoe UI", 16))
        self.heure_pc.pack(anchor="w")
        self.heure_fuseau = ttk.Label(f, foreground="#666")
        self.heure_fuseau.pack(anchor="w", pady=(2, 14))
        self._bouton_action(f, "Mettre la montre à l'heure",
                            self._regler_heure).pack(anchor="w")
        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(f, wraplength=520, foreground="#8a5a00", text=(
            "⚠ Régler l'heure ne recale pas les aiguilles. Leur position est suivie "
            "mécaniquement par la montre et peut dériver de son horloge interne : les "
            "remettre d'aplomb passe par la calibration (rotation des aiguilles jusqu'au "
            "repère de midi), qui se fait sur la montre ou dans l'app officielle. Le "
            "protocole public ne la documente pas."
        )).pack(anchor="w")
        return f

    def _onglet_historiques(self) -> ttk.Frame:
        f = ttk.Frame(self.onglets, padding=12)
        f.rowconfigure(2, weight=1)
        f.columnconfigure(0, weight=1)

        boutons = ttk.Frame(f)
        boutons.grid(row=0, column=0, sticky="w")
        for texte, action in (("Pas", "activity"), ("Sommeil", "sleep"),
                              ("Fréquence cardiaque", "heartrate")):
            b = self._bouton_action(boutons, texte,
                                    lambda a=action, t=texte: self._lire_historique(a, t))
            b.pack(side="left", padx=(0, 6))

        self.hist_resume = ttk.Label(f, text="Aucune lecture", foreground="#666")
        self.hist_resume.grid(row=1, column=0, sticky="w", pady=(10, 4))

        self.hist_texte = tk.Text(f, wrap="none", font=("Consolas", 9), state="disabled")
        self.hist_texte.grid(row=2, column=0, sticky="nsew")
        defil = ttk.Scrollbar(f, command=self.hist_texte.yview)
        defil.grid(row=2, column=1, sticky="ns")
        self.hist_texte.configure(yscrollcommand=defil.set)

        self.bouton_export = ttk.Button(f, text="Enregistrer la trame…",
                                        command=self._exporter, state="disabled")
        self.bouton_export.grid(row=3, column=0, sticky="w", pady=(8, 0))
        return f

    def _onglet_notification(self) -> ttk.Frame:
        f = ttk.Frame(self.onglets, padding=12)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, wraplength=520, foreground="#8a5a00",
                  text=AVERTISSEMENT_NON_TESTE).grid(row=0, column=0, columnspan=2,
                                                     sticky="w", pady=(0, 12))
        ttk.Label(f, text="Type").grid(row=1, column=0, sticky="w", pady=3)
        self.notif_type = tk.StringVar(value="sms")
        ttk.Combobox(f, textvariable=self.notif_type, values=sorted(NOTIF_TYPES),
                     state="readonly", width=18).grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Titre").grid(row=2, column=0, sticky="w", pady=3)
        self.notif_titre = tk.StringVar()
        ttk.Entry(f, textvariable=self.notif_titre).grid(row=2, column=1, sticky="ew")
        ttk.Label(f, text="Message").grid(row=3, column=0, sticky="nw", pady=3)
        self.notif_corps = tk.Text(f, height=4, wrap="word")
        self.notif_corps.grid(row=3, column=1, sticky="ew")
        self._bouton_action(f, "Envoyer la notification",
                            self._envoyer_notification).grid(row=4, column=1,
                                                             sticky="w", pady=(12, 0))
        return f

    def _onglet_meteo(self) -> ttk.Frame:
        f = ttk.Frame(self.onglets, padding=12)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, wraplength=520, foreground="#8a5a00",
                  text=AVERTISSEMENT_NON_TESTE).grid(row=0, column=0, columnspan=2,
                                                     sticky="w", pady=(0, 12))
        self.meteo_now = tk.IntVar(value=18)
        self.meteo_min = tk.IntVar(value=12)
        self.meteo_max = tk.IntVar(value=21)
        for ligne, (libelle, var) in enumerate((("Actuelle (°C)", self.meteo_now),
                                                ("Minimale (°C)", self.meteo_min),
                                                ("Maximale (°C)", self.meteo_max)), start=1):
            ttk.Label(f, text=libelle).grid(row=ligne, column=0, sticky="w", pady=3)
            ttk.Spinbox(f, from_=-50, to=60, width=6,
                        textvariable=var).grid(row=ligne, column=1, sticky="w")
        ttk.Label(f, text="Icône").grid(row=4, column=0, sticky="w", pady=3)
        self.meteo_icone = tk.StringVar(value="cloudy")
        ttk.Combobox(f, textvariable=self.meteo_icone, values=sorted(WEATHER_ICONS),
                     state="readonly", width=12).grid(row=4, column=1, sticky="w")
        ttk.Label(f, text="Ville").grid(row=5, column=0, sticky="w", pady=3)
        self.meteo_ville = tk.StringVar()
        ttk.Entry(f, textvariable=self.meteo_ville).grid(row=5, column=1, sticky="ew")
        self._bouton_action(f, "Envoyer la météo",
                            self._envoyer_meteo).grid(row=6, column=1,
                                                      sticky="w", pady=(12, 0))
        ttk.Label(f, wraplength=520, foreground="#666", text=(
            "La même prévision est envoyée pour les 4 blocs de jours : la structure "
            "exacte de J+1 à J+3 n'est pas documentée publiquement."
        )).grid(row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))
        return f

    # ---- etat de l'interface ----

    def _logger(self, message: str):
        self.journal.configure(state="normal")
        self.journal.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        self.journal.see("end")
        self.journal.configure(state="disabled")

    def _statut(self, texte: str, couleur: str = "#888"):
        self.etat.configure(text=texte, foreground=couleur)

    def _set_occupe(self, occupe: bool):
        self.occupe = occupe
        pret = self.session is not None and not occupe
        for bouton in self.boutons_action:
            bouton.configure(state="normal" if pret else "disabled")
        self.bouton_scan.configure(state="disabled" if occupe else "normal")
        self.bouton_deconnecter.configure(
            state="normal" if (self.session is not None
                               and self.session.persistent and not occupe) else "disabled")
        self.root.configure(cursor="watch" if occupe else "")

    def _tic_horloge(self):
        maintenant = datetime.now()
        self.heure_pc.configure(text=f"{maintenant:%A %d %B %Y — %H:%M:%S}")
        self.heure_fuseau.configure(
            text=f"Fuseau qui sera envoyé : {decalage_lisible(build_time_payload(maintenant))}")
        self.root.after(1000, self._tic_horloge)

    # ---- recherche de montres ----

    def _charger_adresse_enregistree(self):
        adresse = load_local_config().get("address")
        if not adresse:
            return
        self.adresse_enregistree = adresse
        self.appareils[adresse] = {"nom": "(adresse enregistrée)", "rssi": None,
                                   "zetime": True}
        self.liste.insert("", "end", iid=adresse,
                          values=(adresse, "(adresse enregistrée)", "—"), tags=("zetime",))
        self.liste.selection_set(adresse)
        self._logger(f"Adresse lue dans zetime.local : {adresse}")

    def _scanner(self):
        self.liste.delete(*self.liste.get_children())
        self.appareils.clear()
        while not self.scan_queue.empty():
            self.scan_queue.get_nowait()

        duree = self.duree_scan.get()
        self._set_occupe(True)
        self._statut(f"Recherche en cours ({duree} s)…", "#8a5a00")
        self._logger(f"Recherche de périphériques BLE pendant {duree} s. "
                     "Gardez la montre réveillée et à moins d'un mètre.")
        self._vider_file_scan()
        self.runner.submit(self._scan_async(duree), self._scan_fini, self._echec)

    async def _scan_async(self, duree: float):
        def detecte(device, adv):
            self.scan_queue.put({
                "adresse": device.address,
                "nom": adv.local_name or device.name,
                "rssi": adv.rssi,
                "zetime": looks_like_zetime(adv.local_name or device.name, adv),
            })

        scanner = BleakScanner(detection_callback=detecte, scanning_mode="active")
        await scanner.start()
        try:
            await asyncio.sleep(duree)
        finally:
            await scanner.stop()

    def _vider_file_scan(self):
        """Verse dans le tableau ce que le thread asyncio a vu, sans y toucher lui-meme."""
        while not self.scan_queue.empty():
            vu = self.scan_queue.get_nowait()
            adresse = vu["adresse"]
            connu = self.appareils.setdefault(adresse, {"nom": None, "rssi": None,
                                                        "zetime": False})
            # Le nom arrive souvent dans la "scan response", apres l'annonce
            # initiale : on garde le meilleur vu jusqu'ici plutot que le dernier.
            connu["nom"] = vu["nom"] or connu["nom"]
            connu["rssi"] = vu["rssi"]
            connu["zetime"] = (connu["zetime"] or vu["zetime"]
                               or adresse == self.adresse_enregistree)

            nouveau = not self.liste.exists(adresse)
            if self._afficher(connu):
                self._afficher_ligne(adresse, connu)
                if nouveau and connu["zetime"]:
                    self._logger(f"ZeTime détectée : {adresse} ({connu['nom']})")
            elif connu["zetime"] and nouveau:
                self._logger(f"ZeTime détectée : {adresse} ({connu['nom']})")

        self._trier_par_signal()
        if self.occupe:
            self.root.after(300, self._vider_file_scan)

    def _afficher(self, connu: dict) -> bool:
        return bool(connu.get("zetime")) or self.afficher_tout.get()

    def _afficher_ligne(self, adresse: str, connu: dict):
        valeurs = (adresse, connu["nom"] or "(sans nom diffusé)",
                   f"{connu['rssi']} dBm" if connu["rssi"] is not None else "—")
        tags = ("zetime",) if connu.get("zetime") else ()
        if self.liste.exists(adresse):
            self.liste.item(adresse, values=valeurs, tags=tags)
        else:
            self.liste.insert("", "end", iid=adresse, values=valeurs, tags=tags)

    def _rafraichir_liste(self):
        """Reconstruit le tableau quand on coche/décoche « Afficher tous les appareils »."""
        selection = self.liste.selection()
        self.liste.delete(*self.liste.get_children())
        for adresse, connu in self.appareils.items():
            if self._afficher(connu):
                self._afficher_ligne(adresse, connu)
        self._trier_par_signal()
        if selection and self.liste.exists(selection[0]):
            self.liste.selection_set(selection[0])

    def _trier_par_signal(self):
        def force(iid):
            rssi = self.appareils.get(iid, {}).get("rssi")
            return rssi if rssi is not None else -999
        for position, iid in enumerate(sorted(self.liste.get_children(),
                                              key=force, reverse=True)):
            self.liste.move(iid, "", position)

    def _scan_fini(self, _resultat):
        self._set_occupe(False)
        self._vider_file_scan()
        total = len(self.appareils)
        montres = sum(1 for a in self.appareils.values() if a.get("zetime"))
        self._logger(f"Recherche terminée : {total} périphérique(s) BLE vus, {montres} ZeTime.")
        if not montres:
            self._logger("Aucun nom évoquant ZeTime/Kronoz. Une montre déjà connectée "
                         "à un téléphone n'émet plus d'annonce BLE : coupez le Bluetooth "
                         "du téléphone, réveillez l'écran, puis relancez.")
        self._statut("Aucune montre sélectionnée" if self.session is None
                     else self.etat.cget("text"),
                     "#888" if self.session is None else "#0a6d2e")

    # ---- selection et connexion ----

    def _selectionner(self):
        choix = self.liste.selection()
        if not choix:
            messagebox.showinfo("ZeTime", "Sélectionnez d'abord une montre dans la liste.")
            return
        adresse = choix[0]
        maintenu = self.mode_maintenu.get()

        if self.session is not None:
            self.runner.run_blocking(self.session.close())
        self.session = Session(adresse, maintenu)

        if not maintenu:
            self._set_occupe(False)
            self._statut(f"{adresse} — connexion à chaque commande", "#0a6d2e")
            self._logger(f"Montre sélectionnée : {adresse} (connexion par commande).")
            return

        self._set_occupe(True)
        self._statut(f"Connexion à {adresse}…", "#8a5a00")
        self._logger(f"Ouverture de la connexion vers {adresse}…")

        def ok(_):
            self._set_occupe(False)
            self._statut(f"{adresse} — connectée", "#0a6d2e")
            self._logger("Connexion établie et maintenue.")

        self.runner.submit(self.session.connect(), ok, self._echec)

    def _deconnecter(self):
        if self.session is None:
            return
        self.runner.run_blocking(self.session.close())
        self._logger(f"Déconnecté de {self.session.address}.")
        self._statut(f"{self.session.address} — déconnectée", "#888")
        self._set_occupe(False)

    # ---- commandes ----

    def _lancer(self, libelle: str, action: Callable, sur_resultat: Callable):
        if self.session is None:
            return
        self._set_occupe(True)
        self._logger(f"{libelle}…")

        def ok(resultat):
            self._set_occupe(False)
            sur_resultat(resultat)

        self.runner.submit(self.session.run(action), ok, self._echec)

    def _echec(self, exc: Exception):
        self._set_occupe(False)
        self._statut("Échec de la dernière opération", "#a11")
        self._logger(f"ÉCHEC — {type(exc).__name__}: {exc}")
        messagebox.showerror(
            "Échec",
            f"{type(exc).__name__}\n\n{exc}\n\n"
            "Vérifiez que la montre est réveillée, à portée, et qu'elle n'est pas "
            "déjà connectée à un téléphone."
        )

    def _lire_info(self):
        async def action(zt):
            return await zt.get_serial(), await zt.get_info_availability()

        def montrer(resultat):
            serie, dispo = (parse_frame(t) for t in resultat)
            self.info_serie.configure(
                text="Numéro de série : " + (serie[2].decode("ascii", "replace")
                                             if serie else "réponse illisible"))
            if dispo and len(dispo[2]) >= 8:
                a, b = struct.unpack("<II", dispo[2][:8])
                self.info_donnees.configure(
                    text=f"Données en attente : compteur 1 = {a}, compteur 2 = {b}")
            else:
                self.info_donnees.configure(text="Données en attente : réponse illisible")
            self._logger("Informations lues.")

        self._lancer("Lecture des informations", action, montrer)

    def _regler_heure(self):
        maintenant = datetime.now()
        fuseau = decalage_lisible(build_time_payload(maintenant))

        def fini(_):
            self._logger(f"Heure envoyée : {maintenant:%d/%m/%Y %H:%M:%S} ({fuseau}). "
                         "Les aiguilles, elles, se recalent par la calibration.")

        self._lancer("Réglage de l'heure", lambda zt: zt.sync_time(), fini)

    def _lire_historique(self, nom_methode: str, libelle: str):
        methodes = {"activity": "get_activity", "sleep": "get_sleep",
                    "heartrate": "get_heartrate"}

        def montrer(trame):
            if not trame:
                self.hist_resume.configure(text=f"{libelle} : aucune réponse reçue")
                self._logger(f"{libelle} : pas de réponse avant expiration du délai.")
                return
            self.dernier_brut = trame
            self.dernier_brut_nom = nom_methode
            analysee = parse_frame(trame)
            charge = len(analysee[2]) if analysee else 0
            self.hist_resume.configure(
                text=f"{libelle} — {len(trame)} octets de trame, "
                     f"{charge} de charge utile — format non décodé")
            self.hist_texte.configure(state="normal")
            self.hist_texte.delete("1.0", "end")
            self.hist_texte.insert("1.0", hexdump(trame))
            self.hist_texte.configure(state="disabled")
            self.bouton_export.configure(state="normal")
            self._logger(f"{libelle} : {len(trame)} octets reçus.")

        self._lancer(f"Lecture — {libelle}",
                     lambda zt: getattr(zt, methodes[nom_methode])(), montrer)

    def _exporter(self):
        if not self.dernier_brut:
            return
        defaut = f"zetime_{self.dernier_brut_nom}_{datetime.now():%Y%m%d_%H%M%S}.bin"
        chemin = filedialog.asksaveasfilename(
            defaultextension=".bin", initialfile=defaut,
            filetypes=[("Trame brute", "*.bin"), ("Tous les fichiers", "*.*")])
        if not chemin:
            return
        Path(chemin).write_bytes(self.dernier_brut)
        self._logger(f"Trame enregistrée : {chemin}")

    def _envoyer_notification(self):
        titre = self.notif_titre.get()
        corps = self.notif_corps.get("1.0", "end").strip()
        kind = self.notif_type.get()
        self._lancer("Envoi de la notification",
                     lambda zt: zt.push_notification(kind, titre, corps),
                     lambda _: self._logger(f"Notification « {kind} » envoyée."))

    def _envoyer_meteo(self):
        args = (self.meteo_now.get(), self.meteo_min.get(), self.meteo_max.get(),
                self.meteo_icone.get(), self.meteo_ville.get())
        self._lancer("Envoi de la météo",
                     lambda zt: zt.push_weather(*args),
                     lambda _: self._logger("Météo envoyée."))

    # ---- fermeture ----

    def _fermer(self):
        if self.session is not None:
            self.runner.run_blocking(self.session.close())
        self.runner.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")  # rendu Windows natif quand il est disponible
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
