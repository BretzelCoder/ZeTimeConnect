"""
test_zetime.py -- tests de la logique de trames de zetime_ctl.py, SANS montre.

Ce qui est testable hors materiel, c'est la couche protocole : construction et
relecture des trames, reassemblage des notifications, separation des deux flux
BLE et filtrage des reponses. Tout ce qui touche a bleak est hors de portee ici
(ZeTime est instancie avec client=None : aucun test ci-dessous n'emet).

    python test_zetime.py

Sortie : une ligne "ok" par test, et un code de retour non nul si l'un echoue.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from zetime_ctl import ZeTime, build_frame, build_time_payload, chunk_frame, parse_frame

TESTS = []


def test(nom):
    def deco(fn):
        TESTS.append((nom, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# Trames : construction et relecture
# ---------------------------------------------------------------------------

@test("build_frame / parse_frame font l'aller-retour")
async def _():
    frame = build_frame(0x02, 0x70, b"\x00")
    assert frame[0] == 0x6F and frame[-1] == 0x8F, "preambule ou terminateur absent"
    assert parse_frame(frame) == (0x02, 0x70, b"\x00")


@test("parse_frame rejette une trame tronquee ou mal formee")
async def _():
    assert parse_frame(b"") is None
    assert parse_frame(None) is None
    assert parse_frame(b"\x6f\x02\x70\x01\x00") is None       # trop courte
    assert parse_frame(b"\x00\x02\x70\x01\x00\x00\x8f") is None  # mauvais preambule
    assert parse_frame(b"\x6f\x02\x70\x01\x00\x00\x00") is None  # mauvais terminateur


@test("reponse reelle de la montre (numero de serie) relue correctement")
async def _():
    # Trame reellement capturee sur une ZeTime, numero de serie anonymise :
    # seuls les chiffres ont ete remplaces, la structure est celle de l'original
    # (12 octets de payload, soit une longueur annoncee de 0x000c).
    brut = b"o\x02\x80\x0c\x00000000000000\x8f"
    subject, type_, payload = parse_frame(brut)
    assert (subject, type_) == (0x02, 0x80)
    assert payload == b"000000000000"


# ---------------------------------------------------------------------------
# Reglage de l'heure : le fuseau est calcule, pas fige
# ---------------------------------------------------------------------------

@test("payload d'heure : date, heure et fuseau aux bons octets")
async def _():
    when = datetime(2026, 8, 6, 14, 5, 30, tzinfo=timezone(timedelta(hours=2)))
    payload = build_time_payload(when)
    assert len(payload) == 12, f"la montre attend 12 octets, recu {len(payload)}"
    assert payload[:2] == b"\xea\x07", "annee sur 2 octets little-endian (2026 = 0x07ea)"
    assert list(payload[2:7]) == [8, 6, 14, 5, 30], "mois, jour, heure, minute, seconde"
    assert list(payload[7:10]) == [0x00, 0x00, 0x01], "24h / post-calibration / unite"
    assert payload[10] == 2, "UTC+2 doit donner 2, pas le 0x01 fige de la doc"
    assert payload[11] == 0, "pas de minutes de decalage a UTC+2"


@test("payload d'heure : heure d'hiver et heure d'ete ne donnent pas le meme octet")
async def _():
    hiver = build_time_payload(datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=1))))
    ete = build_time_payload(datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))))
    assert (hiver[10], ete[10]) == (1, 2), "c'est ce decalage fige a 1 qui retardait la montre"


@test("payload d'heure : fuseaux negatifs et a la demie")
async def _():
    ny = build_time_payload(datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5))))
    assert ny[10] == 0xFB, "UTC-5 en complement a deux sur un octet"
    assert ny[11] == 0

    delhi = build_time_payload(datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    assert (delhi[10], delhi[11]) == (5, 30), "les 30 min vont dans l'octet des minutes"

    marquises = build_time_payload(datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=-9, minutes=-30))))
    assert (marquises[10], marquises[11]) == (0xF7, 30), "signe sur les heures, minutes en absolu"


@test("payload d'heure : un datetime naif prend le fuseau local sans decaler l'heure")
async def _():
    naif = datetime(2026, 8, 6, 14, 5, 30)
    payload = build_time_payload(naif)
    assert list(payload[2:7]) == [8, 6, 14, 5, 30], "14:05:30 doit rester 14:05:30"


# ---------------------------------------------------------------------------
# Reassemblage : une notification n'est pas une trame
# ---------------------------------------------------------------------------

@test("trame courte : une notification, une trame")
async def _():
    zt = ZeTime(client=None)
    zt._on_ack(None, bytearray(build_frame(0x02, 0x80, b"000000000000")))
    assert zt._responses.qsize() == 1
    assert parse_frame(zt._responses.get_nowait())[2] == b"000000000000"


@test("trame longue : 4 notifications de 20 octets donnent UNE trame")
async def _():
    zt = ZeTime(client=None)
    longue = build_frame(0x54, 0x80, bytes(range(60)))
    morceaux = list(chunk_frame(longue))
    assert len(morceaux) == 4, "le test suppose un decoupage en 4"
    for part in morceaux:
        zt._on_ack(None, bytearray(part))
    assert zt._responses.qsize() == 1, "les morceaux doivent etre recolles, pas empiles"
    assert zt._responses.get_nowait() == longue


@test("deux trames collees dans une meme notification sont separees")
async def _():
    zt = ZeTime(client=None)
    a, b = build_frame(0x02, 0x80, b"AAA"), build_frame(0x52, 0x80, b"BBB")
    zt._on_ack(None, bytearray(a + b))
    assert zt._responses.qsize() == 2
    assert zt._responses.get_nowait() == a
    assert zt._responses.get_nowait() == b


@test("octets parasites avant le preambule : resynchronisation")
async def _():
    zt = ZeTime(client=None)
    zt._on_ack(None, bytearray(b"\x00\xff" + build_frame(0x02, 0x80, b"resync")))
    assert parse_frame(zt._responses.get_nowait())[2] == b"resync"


@test("longueur aberrante : pas de blocage, la trame suivante passe")
async def _():
    zt = ZeTime(client=None)
    zt._on_ack(None, bytearray(b"\x6f\x02\x80\xff\xff"))  # annonce 65535 octets
    zt._on_ack(None, bytearray(build_frame(0x02, 0x80, b"ok")))
    assert zt._responses.qsize() == 1
    assert parse_frame(zt._responses.get_nowait())[2] == b"ok"


# ---------------------------------------------------------------------------
# Separation des flux et filtrage
# ---------------------------------------------------------------------------

@test("les deux flux BLE ne se melangent pas")
async def _():
    zt = ZeTime(client=None)
    zt._on_watch(None, bytearray(build_frame(0x71, 0x80, b"music")))
    assert zt._responses.qsize() == 0, "une requete montre ne doit PAS aller dans les reponses"
    assert zt._watch_requests.qsize() == 1


@test("next_watch_request rend la requete initiee par la montre")
async def _():
    zt = ZeTime(client=None)
    zt._on_watch(None, bytearray(build_frame(0x71, 0x80, b"music")))
    assert parse_frame(await zt.next_watch_request(timeout=1.0))[2] == b"music"
    assert await zt.next_watch_request(timeout=0.2) is None


@test("une reponse au mauvais subject est ignoree, la bonne est rendue")
async def _():
    zt = ZeTime(client=None)
    zt._on_ack(None, bytearray(build_frame(0x99, 0x80, b"vieux")))
    zt._on_ack(None, bytearray(build_frame(0x02, 0x80, b"bon")))
    assert parse_frame(await zt._await_response(0x02, timeout=1.0))[2] == b"bon"


@test("aucune trame au bon subject : expiration du delai")
async def _():
    zt = ZeTime(client=None)
    zt._on_ack(None, bytearray(build_frame(0x99, 0x80, b"parasite")))
    assert await zt._await_response(0x02, timeout=0.3) is None


# ---------------------------------------------------------------------------

async def main():
    echecs = 0
    for nom, fn in TESTS:
        try:
            await fn()
        except AssertionError as e:
            echecs += 1
            print(f"ECHEC  {nom}\n       {e}")
        except Exception as e:  # erreur de test elle-meme, pas une assertion
            echecs += 1
            print(f"ERREUR {nom}\n       {type(e).__name__}: {e}")
        else:
            print(f"ok     {nom}")

    total = len(TESTS)
    print(f"\n{total - echecs}/{total} tests passent.")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
