# ZeTimeConnect

Client Bluetooth LE minimaliste pour la montre **MyKronoz ZeTime**, utilisable sous
Windows sans l'application officielle myKronoz.

L'application myKronoz est fermée : pas de code source public, pas de SDK. Ce projet
réimplémente en Python le protocole BLE propriétaire de la ZeTime, à partir de
l'analyse publiée par le projet communautaire [Gadgetbridge](https://gadgetbridge.org/internals/specifics/zetime-protocol/)
(rédigée par Sauce Maison et Sebastian Kranz). Le code Python est original — il est
écrit d'après cette documentation de protocole, ce n'est pas une traduction du code
Java de Gadgetbridge.

Un seul fichier, une seule dépendance.

## État du projet

| Commande | Effet | Validé sur matériel |
|---|---|---|
| `scan` | Cherche la montre par annonce BLE | ✅ oui |
| `discover` | Liste services et caractéristiques GATT | ✅ oui |
| `info` | Numéro de série, données en attente | ✅ oui |
| `activity` | Historique de pas (brut) | ⚠️ non testé |
| `sleep` | Historique de sommeil (brut) | ⚠️ non testé |
| `heartrate` | Historique de fréquence cardiaque (brut) | ⚠️ non testé |
| `synctime` | **Écrit** l'heure sur la montre | ✅ oui (voir ci-dessous) |
| `notify` | **Envoie** une notification | ⚠️ non testé |
| `weather` | **Envoie** une météo | ⚠️ non testé |

Les commandes de lecture ont été validées contre une ZeTime réelle (firmware de
2017) depuis Windows 11 avec bleak 3.0.2 : la montre répond bien au
protocole décrit ici, et les quatre caractéristiques BLE correspondent exactement à
la documentation Gadgetbridge.

`synctime` a été validée sur la même montre : l'heure affichée change bien.
`notify` et `weather` utilisent la même plomberie validée, mais leur charge utile
n'a jamais été envoyée à une montre.

### Fuseau horaire, et pourquoi les aiguilles peuvent rester fausses

La page de protocole de Gadgetbridge présente les cinq octets qui suivent les
secondes comme non expliqués et conseille la constante `00 00 01 01 00`. Le code
source de Gadgetbridge, lui, les nomme : format 24 h, « SetTime after
calibration », unité, **décalage horaire en heures**, décalage en minutes. Le
quatrième octet de cette constante vaut donc UTC+1 en dur, et l'envoyer tel quel
décale la montre d'une heure dès qu'on n'est pas à UTC+1 — heure d'été européenne
comprise. `build_time_payload()` le calcule depuis le fuseau du système.

Reste une limite qui ne vient pas de ce script : régler l'heure ne recale pas les
**aiguilles**. Leur position est suivie mécaniquement par la montre et peut
dériver de son horloge interne ; la remettre d'aplomb demande la procédure de
calibration (faire tourner les aiguilles jusqu'au repère de midi), que l'app
officielle expose et que le protocole public ne documente pas. Si l'affichage
numérique est juste mais que les aiguilles ne le sont pas, c'est ce cas-là :
passer par la calibration de la montre ou de l'app myKronoz.

## Installation

Prérequis : Windows 10/11, Python 3.9 ou plus récent, une puce Bluetooth LE
activée. Vérifiez Python et l'accès au Bluetooth avant d'aller plus loin :

```
python --version
```

Si la commande est inconnue, installez Python depuis
[python.org](https://www.python.org/downloads/windows/) en cochant *Add python.exe
to PATH*. Côté Bluetooth, *Paramètres → Bluetooth et appareils* doit afficher un
adaptateur actif ; sans lui, aucune commande de ce projet ne fonctionnera.

Récupérez le dépôt et installez la dépendance, de préférence dans un
environnement virtuel pour ne rien ajouter au Python du système :

```
git clone https://github.com/BretzelCoder/ZeTimeConnect.git
cd ZeTimeConnect

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

À chaque nouvelle session de terminal, réactivez l'environnement avec
`.venv\Scripts\activate` avant de lancer un script.

### Dépendances

Une seule dépendance directe, `bleak`, épinglée à la version exacte validée sur
matériel. Tkinter, dont dépend l'interface graphique, est livré avec Python sous
Windows et ne s'installe pas par pip.

| Fichier | Contenu | Quand l'utiliser |
|---|---|---|
| `requirements.txt` | `bleak==3.0.2` | Cas normal. pip résout le backend BLE adapté à votre système. |
| `requirements-lock.txt` | les 11 paquets de l'arbre complet, figés | Pour rejouer à l'identique l'environnement de validation. Windows uniquement. |

L'arbre complet est dans un fichier séparé parce qu'il est **spécifique à la
plateforme** : le backend BLE de bleak est `winrt-*` sous Windows, `pyobjc-*` sous
macOS, `dbus-fast` sous Linux. Un lock figé sous Windows est donc ininstallable
ailleurs, alors que `requirements.txt` laisse pip choisir.

Le lock a été vérifié dans un environnement vierge : `pip install -r
requirements-lock.txt` installe 11 paquets et rien d'autre, et `test_zetime.py`
y passe ses 16 tests.

Versions de référence : Python 3.14.3, bleak 3.0.2, Windows 11. Le plancher réel
est Python 3.9 ; rien dans le code n'exige de syntaxe plus récente.

Vérifiez l'installation sans avoir besoin de la montre :

```
python test_zetime.py
```

Les 16 tests de la couche protocole doivent passer. S'ils échouent ici, le
problème est dans l'installation, pas dans le Bluetooth.

### Premier démarrage

Réveillez l'écran de la montre, gardez-la à moins d'un mètre, et coupez le
Bluetooth du téléphone s'il y est appairé (voir
[La montre n'apparaît pas dans le scan](#la-montre-napparaît-pas-dans-le-scan)).

1. **Trouver la montre.** `python zetime_ctl.py scan` — elle apparaît sous un nom
   du type `ZeTime#12345`. Relevez son adresse.
2. **Enregistrer son adresse.** `copy zetime.local.example zetime.local`, puis
   remplacez-y `AA:BB:CC:DD:EE:FF` par l'adresse relevée. Les commandes n'auront
   plus besoin de `--address`. Ce fichier est ignoré par git.
3. **Vérifier le dialogue.** `python zetime_ctl.py discover` liste les
   caractéristiques GATT et confirme qu'elles correspondent aux constantes du
   script, puis `python zetime_ctl.py info` affiche le numéro de série. À faire
   avant toute commande qui écrit sur la montre.

De là, `python zetime_gui.py` ouvre l'interface graphique, ou continuez en ligne
de commande — les deux sections suivantes détaillent l'une et l'autre.

## Interface graphique

```
python zetime_gui.py
```

Cherche les montres à proximité, en sélectionne une, puis lance les commandes
dessus depuis cinq onglets : Informations, Heure, Historiques, Notification,
Météo. Si `zetime.local` contient une adresse, elle est proposée d'emblée : pas
besoin de scanner pour une montre déjà connue.

`zetime_gui.py` n'implémente aucune trame — toute la logique BLE vient de
`zetime_ctl.py`, l'interface ne fait que l'habiller. Elle n'ajoute aucune
dépendance : Tkinter est livré avec Python sous Windows.

**Mode de connexion**, à choisir avant de sélectionner la montre :

- *une connexion par commande* (défaut) — comme le CLI : chaque bouton ouvre puis
  referme le lien. Compter 2 à 5 s par commande, mais rien à maintenir.
- *garder la connexion ouverte* — les commandes s'enchaînent sans attente. Si la
  montre s'est éloignée entre-temps, la commande suivante se reconnecte seule.

Les historiques (pas, sommeil, cardio) s'affichent en hexadécimal avec un bouton
d'export : leur format n'est pas décodé, autant garder les captures brutes plutôt
que d'afficher des chiffres déduits comme s'ils étaient sûrs. Les onglets
Notification et Météo portent un avertissement visible tant que leur charge utile
n'a pas été validée sur matériel.

Note d'architecture : `bleak` est asyncio et Tkinter a sa propre boucle bloquante.
La boucle asyncio tourne donc dans un thread démon, et les résultats reviennent à
l'interface via `root.after()` — aucun widget n'est touché depuis le thread
asyncio.

## Utilisation en ligne de commande

Trouver la montre :

```
python zetime_ctl.py scan
python zetime_ctl.py scan --timeout 30
```

Le scan affiche, pour chaque périphérique vu, son adresse, la puissance du signal,
les UUID de services annoncés et les données fabricant. La montre apparaît sous un
nom du type `ZeTime#12345` et est signalée dès qu'elle est détectée.

Notez son adresse et enregistrez-la une fois pour toutes (voir
[Adresse de la montre](#adresse-de-la-montre)) :

```
copy zetime.local.example zetime.local
```

puis remplacez-y `AA:BB:CC:DD:EE:FF` par l'adresse relevée. Les commandes n'ont
alors plus besoin de `--address` :

```
python zetime_ctl.py discover
python zetime_ctl.py info
python zetime_ctl.py activity
python zetime_ctl.py sleep
python zetime_ctl.py heartrate
python zetime_ctl.py synctime

python zetime_ctl.py notify  --type sms --title "Maman" --body "Bien arrive ?"
python zetime_ctl.py weather --now 18 --min 12 --max 21 --icon cloudy --city "Geneve"
```

Types de notification acceptés : `missed_call`, `sms`, `email`, `call`, `call_end`,
`viber`, `snapchat`, `whatsapp`, `facebook`, `hangout`, `gmail`, `messenger`,
`instagram`. Icônes météo : `cloudy`, `rainy`, `stormy`.

**Lancez toujours `discover` en premier** sur une montre inconnue, pour vérifier que
ses caractéristiques correspondent aux constantes du script avant d'écrire quoi que
ce soit.

## Adresse de la montre

Une adresse MAC identifie un appareil physique précis : elle n'a rien à faire dans un
dépôt public. Elle est donc lue depuis **`zetime.local`**, un fichier placé à côté du
script et ignoré par git.

```
# zetime.local
address = AA:BB:CC:DD:EE:FF
```

Format : `clé = valeur`, un par ligne, `#` en commentaire. Le modèle
`zetime.local.example` est versionné et sert de documentation ; `zetime.local`, lui,
ne l'est pas — c'est la seule copie qui contient votre vraie adresse.

Ordre de priorité : `--address` s'il est passé explicitement, sinon `zetime.local`,
sinon un message d'erreur qui rappelle les deux options. `--address` reste donc utile
pour viser ponctuellement une autre montre que la vôtre. `scan` est la seule commande
qui n'en a jamais besoin — c'est elle qui sert à trouver l'adresse.

Le fichier est cherché à côté de `zetime_ctl.py`, pas dans le répertoire courant :
les commandes fonctionnent depuis n'importe où.

## La montre n'apparaît pas dans le scan

Dans l'ordre de probabilité :

1. **Elle est connectée à votre téléphone.** Une ZeTime qui tient un lien avec
   l'application myKronoz cesse complètement d'émettre : Windows ne peut pas la voir.
   Coupez le Bluetooth du téléphone, ou faites-lui oublier la montre.
2. **Elle dort.** Réveillez l'écran juste avant le scan, et gardez-la à moins d'un
   mètre pendant toute sa durée.
3. **Elle n'est pas en mode appairage.** Cherchez l'entrée Bluetooth / appairage dans
   les menus de la montre.

Un périphérique sans nom mais au signal fort qui n'apparaît que quand la montre est
réveillée est un bon candidat : testez-le avec `discover`.

## Protocole

### Trames

```
6f | subject | type | longueur (2 octets, little-endian) | payload | 8f
```

`0x6f` en préambule, `0x8f` en terminateur. Le champ `type` vaut `0x70` pour une
requête de lecture, `0x71` pour une écriture, et `0x80` dans les réponses de la
montre. Le `subject` de la réponse reprend celui de la requête.

Les trames sont découpées en écritures GATT de 20 octets maximum.

### Caractéristiques BLE

Service `00006006-0000-1000-8000-00805f9b34fb` :

| UUID | Rôle | Handle de valeur |
|---|---|---|
| `00008001-…` | Téléphone → montre : corps du message | `0x0012` |
| `00008002-…` | Validation (écrire `0x03`) ; **et** réponses de la montre en notification | `0x0016` |
| `00008003-…` | Téléphone → montre : réponses aux requêtes de la montre | `0x001a` |
| `00008004-…` | Montre → téléphone : requêtes initiées par la montre, en notification | `0x001e` |

Deux pièges valent d'être signalés, tous deux vérifiés sur matériel :

**Handles de déclaration contre handles de valeur.** La documentation Gadgetbridge
cite les handles de *valeur* (`0x0012`, `0x0016`, `0x001a`, `0x001e`), alors que
bleak sous Windows expose les handles de *déclaration*, soit un de moins (`0x0011`,
`0x0015`, `0x0019`, `0x001d`). Passer `0x0012` à bleak échoue à résoudre la
caractéristique. Le script les adresse donc par UUID, ce qui est non ambigu quelle
que soit la plateforme.

**Aucune des quatre caractéristiques n'a la propriété `write`**, seulement
`write-without-response`. Toute écriture doit être non acquittée au niveau ATT
(`response=False`). Comme rien ne régule alors le débit, le script marque une pause
de 20 ms entre les morceaux d'une trame longue, faute de quoi la montre en perd.

### Deux flux de notification, à ne pas confondre

`8002` porte les réponses de la montre à *nos* commandes. `8004` porte les requêtes
que la montre initie *seule* (contrôle musique, « retrouver mon téléphone », rejet
d'appel). Les verser dans une file commune ferait consommer une requête spontanée à
la place de la réponse attendue, si la montre parle pendant qu'on l'interroge. Le
script maintient donc deux files distinctes, et `send()` ignore les trames dont le
`subject` ne correspond pas à la requête en cours.

Enfin, **une notification n'est pas une trame** : au-delà de 20 octets la montre
découpe sa réponse. Le script accumule par flux et ne publie qu'une trame entière,
dont la longueur est donnée par l'en-tête.

## Tests

```
python test_zetime.py
```

16 tests de la couche protocole, sans montre : aller-retour des trames, réassemblage
des notifications, séparation des deux flux, filtrage par `subject`,
resynchronisation après octets parasites. `ZeTime` y est instancié avec
`client=None`, aucun test n'émet sur le BLE. Code de retour non nul si l'un échoue.

L'un des tests rejoue une réponse réellement capturée sur la montre, comme garde-fou
de régression ancré sur du matériel plutôt que sur une lecture de la documentation.

## Volontairement non implémenté

Ces fonctions ne sont pas décrites de façon fiable par la source publique. Mieux vaut
ne rien envoyer qu'inventer un octet au hasard sur un appareil réel :

- niveau de batterie ;
- profil utilisateur (taille, poids, âge) et objectifs (pas, calories) ;
- mise à jour du firmware — personne ne la supporte, Gadgetbridge compris ;
- réponses aux requêtes initiées par la montre (caractéristique `8003`) : le flux est
  lisible via `next_watch_request()`, mais les formats de réponse manquent.

La météo multi-jours est simplifiée : la même prévision est envoyée pour les quatre
blocs (jour courant plus trois jours), faute de détail public sur la structure exacte
des jours J+1 à J+3.

## Crédits et licence

Le protocole provient de la documentation de [Gadgetbridge](https://gadgetbridge.org/internals/specifics/zetime-protocol/)
(AGPLv3), analyse de Sauce Maison et Sebastian Kranz. Merci à eux : sans ce travail
de rétro-ingénierie publié, ce projet n'existerait pas.

Le code de ce dépôt est original et distribué sous licence MIT (voir [LICENSE](LICENSE)).
Seule la description du protocole vient de Gadgetbridge ; aucune ligne de son code
Java n'a été reprise.

Projet personnel, sans aucun lien avec MyKronoz. « ZeTime » et « myKronoz » sont des
marques de leurs propriétaires respectifs.
