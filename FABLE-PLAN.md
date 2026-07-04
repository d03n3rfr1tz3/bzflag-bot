# BZFlag-Bot: Analyse & Übergabeplan (Performance / Fehlerpotenzial / Wartbarkeit)

## Kontext

Der BZFlag-Bot (Python, ~10.600 Zeilen) läuft lokal mit <1–7% CPU, auf dem Online-Server jedoch mit 30–50%. Dieser Plan ist das Ergebnis einer unvoreingenommenen Analyse (Profiling-Daten aus `tmp/cprofile_bzbot.prof` + `tmp/flame_bzbot.svg`, Code-Review der Hot-Paths) und dient als **Übergabedokument an Junior-Entwickler bzw. Sonnet-Level-Modelle**. Jede Maßnahme ist so beschrieben, dass sie eigenständig und isoliert umsetzbar ist: Was, Wo (Datei:Zeile), Wie, erwartetes Potenzial, Risiko, Verifikation.

**Priorität und Reihenfolge der Themen: 1. Performance → 2. Fehlerpotenzial → 3. Wartbarkeit.** Die Wartbarkeits-Umstrukturierung läuft bewusst **zuletzt** (Details unter „Wechselwirkungen" in Teil 3): Alle Datei:Zeile-Angaben in diesem Plan beziehen sich auf den Stand vor jeder Umstrukturierung (Commit `97b0399`). Solange Performance- und Fehler-Maßnahmen offen sind, bleiben diese Referenzen dadurch gültig. Stabile Anker über Umbauten hinweg sind die **Methodennamen** — jeder Punkt nennt sie explizit, sie ändern sich in keinem Schritt dieses Plans.

**Einordnung Lokal- vs. Server-Messung (wichtig):** Beide Profile wurden lokal aufgenommen — aber auf **derselben Karte (HIX: 57 Layer, 41.372 Nav-Knoten, hunderte Obstacles) mit effektiv denselben Einstellungen** (Flaggen, Schussanzahl, Server-Variablen) und ähnlicher Bot-/Spielerzahl wie auf dem Server; verbunden wird in beiden Fällen über localhost. Die Diskrepanz (<1–7% lokal vs. 30–50% Server) erklärt sich also NICHT durch unterschiedliche Last, sondern primär durch: (a) **deutlich leistungsschwächere Server-Hardware** — die ~6,7% aktive CPU eines schnellen lokalen Kerns entsprechen auf einem schwachen (v)CPU-Kern schnell 30–50%; (b) **Laufzeit-Effekte**: Server-Bots laufen stundenlang, der F3-Leak (Teil 2) lässt CPU/Speicher über die Uptime wachsen, während Lokal-Läufe nur 2 Minuten dauern; (c) **Situations-Varianz** durch echte Spieler (Gefechtsphasen mit mehr gleichzeitigen Schüssen/Dodges). Konsequenz: Die **relative Verteilung** der Hot-Paths aus dem lokalen Profil ist auf den Server übertragbar (gleiche Karte, gleiche Settings) — nur die absoluten Prozente nicht. Jede relative Einsparung wirkt auf dem Server also ~1:1; zusätzlich adressieren die F-Punkte die Uptime-Komponente. Die teuersten Codepfade skalieren **linear mit Hindernis-, Schuss- und Spielerzahl** — auf der hindernisreichen HIX liegt dort der algorithmische Hebel. Von den 304s des cProfile-Laufs sind >290s Blocking-Waits (Socket-recv, Event.wait der 60-Hz-Schleife) — die echte Compute-Zeit lokal ist klein, ihre Verteilung aber aussagekräftig.

**Profil-Befund (py-spy, 2004 aktive Samples; cProfile 304s):**

| Hot-Path | Anteil aktive CPU | Skaliert mit |
|---|---|---|
| `_update_movement` → `_dispatch_movement` (60 Hz) | ~53% | konstant |
| davon: `_execute_combat_move` inkl. LoS/Ricochet-Gates | ~12–24% | Hindernisse |
| `_has_los_to_enemy`/`_has_los_to_point`/`_segment_clear` | ~19% (Summe) | Hindernisse |
| `_send_update` → UDP `sendto` (30 Hz, Syscall) | ~15% | konstant |
| Ricochet-Sweep (`_compute_ricochet_aim` → `simulate_shot_path`) | ~8% | **Hindernisse × Winkel** |
| `_run_physics` + `_get_floor_z` | ~13% | Hindernisse (gegridded) |
| `_resolve_incoming_shots` (60 Hz, pro Schuss) | ~5% | **Schüsse × Segmente** |
| cProfile: `ray_pyramid_hit` 468k + `ray_box_hit` 484k Calls, `_time_ray_hits_plane` 1,23M Calls, 6,7M `abs()` | — | Hindernisse |

---

## Teil 1: Performance

Priorisiert nach **Server-Potenzial ÷ Risiko**. Reihenfolge ist zugleich empfohlene Umsetzungsreihenfolge; nach P1–P4 auf dem Server neu messen (py-spy, s. „Messmethodik" unten), bevor P6+ angegangen wird.

### P1 — Broad-Phase-Grid für `simulate_shot_path` ⭐ größter Hebel — ✅ umgesetzt (Branch `perf/track2`, Commit `35bec5f`; Benchmark HIX 300 Rico-Schüsse: 62ms → 8ms ≈ 8×, Äquivalenz 0 Mismatch über 600 Random-Schüsse inkl. HIX)

**Problem:** `simulate_shot_path` ([shot_physics.py:691](bzflag/shot_physics.py#L691)) iteriert pro Bounce-Iteration **linear über alle** nicht-durchschießbaren Obstacles (`ray_pyramid_hit`/`ray_box_hit` je Obstacle). Aufrufer:
1. **Defensiv** — [bzbot.py:1340](bzbot.py#L1340) `_on_shot_begin`: pro gegnerischem Schuss, mit `max_bounces=100` (Default). Auf Ricochet-Servern prallt ein Schuss real 10–30× ab → 10–30 volle Obstacle-Scans **pro Schuss**, bei vielen Spielern ständig.
2. **Offensiv** — [bzbot_ai.py:3458](bzbot_ai.py#L3458) `_compute_ricochet_aim`: ~80–110 Kandidaten-Winkel × `max_bounces=3` alle 2s (`RICO_AIM_CACHE_TTL`) pro Ziel → 240–440 Obstacle-Scans pro Sweep.

Schon im 2-Minuten-Lokallauf auf HIX sind das 468k+484k Ray-Tests — **der größte einzelne Compute-Posten des Profils**. Auf der leistungsschwächeren Server-Hardware ist genau dieser Posten der teuerste; er skaliert zudem linear mit der Schussfrequenz (Gefechtsphasen mit echten Spielern).

**Lösung:** Die vorhandene `ObstacleGrid`-Infrastruktur ([nav_graph.py:154](bzflag/nav_graph.py#L154), DDA-`query_ray` [nav_graph.py:216](bzflag/nav_graph.py#L216)) wiederverwenden:
- Neuen optionalen Parameter `obs_grid: ObstacleGrid | None = None` in `simulate_shot_path` einführen. Wenn gesetzt: pro Bounce-Iteration `obs_grid.query_ray(ox, oy, ox + ddx*time_left, oy + ddy*time_left)` statt `test_obs` iterieren. Das 2D-Grid ist korrekt, weil die DDA alle in XY überflogenen Zellen liefert und die Narrow-Phase (exaktes t, min-über-Kandidaten) unverändert bleibt — Ergebnis identisch, nur Kandidatenmenge kleiner (Grid-`pad` garantiert keine False Negatives, dokumentiert in [nav_graph.py:162](bzflag/nav_graph.py#L162)).
- Grid einmalig beim Welt-Laden bauen: `ObstacleGrid([o for o in world_map.boxes if not o.shoot_through])`, z.B. als `self._shot_grid` in `_on_world_ready` ([bzbot.py:866](bzbot.py#L866)) oder direkt als Attribut an `WorldMap`. An beide Aufrufer durchreichen.
- Ohne `obs_grid` (Tests, Fallback): bisheriger linearer Pfad — byte-identisches Verhalten.
- Voraussetzung: `ObstacleGrid` muss aus `nav_graph.py` nach `bzflag/obstacle_grid.py` herausgelöst werden (sonst Importzyklus nav_graph↔shot_physics) — das ist Maßnahme **W6 aus Teil 3**, sie wird hierfür in den Performance-Track vorgezogen.

**Potenzial:** Hoch (geschätzt 5–20× auf den beiden heißesten Compute-Pfaden; HIX hat hunderte Obstacles, das Grid reduziert auf wenige Kandidaten pro überflogener Zelle). Der relative Gewinn ist lokal wie auf dem Server messbar (gleiche Karte) — absolut zählt er auf dem schwächeren Server-Host am meisten.
**Risiko:** Niedrig–mittel. Semantik bleibt exakt (Narrow-Phase unverändert); Restrisiko nur in der Kandidaten-Vollständigkeit → durch Äquivalenztest abgedeckt.
**Verifikation:** Äquivalenztest nach dem Muster `TestLosRayGridEquivalence`/`TestGetFloorZGridEquivalence` (tests/test_performance.py bzw. test_nav_graph.py): auf einer echten Karte (inkl. Pyramiden, Teleporter, rotierte Boxen) einige tausend zufällige Schuss-Parameter simulieren, Segmentlisten alt vs. neu **exakt** vergleichen (0 Mismatch). Bestehende `test_shot_physics.py` muss grün bleiben.

### P2 — `test_obs`-Filterliste vorberechnen (Quick Win) — ✅ umgesetzt (Branch `perf/track2`, Commit `0d4ab1c`; `WorldMap.solid_obstacles()` + `solid_obs`-Parameter)

**Problem:** `test_obs = [o for o in obstacles if not o.shoot_through]` ([shot_physics.py:664](bzflag/shot_physics.py#L664)) wird bei **jedem** der 4.749 Aufrufe neu gebaut (List-Comprehension über alle Obstacles).
**Lösung:** Fällt mit P1 weg (Grid ersetzt die Liste); falls P1 verschoben wird: gefilterte Liste einmalig am `WorldMap` cachen und übergeben.
**Potenzial:** Klein lokal, moderat auf großen Karten. **Risiko:** Null. **Verifikation:** bestehende Tests.

### P3 — `simulate_shot_path` aus dem `_shots_lock` herausziehen — ✅ umgesetzt (Branch `perf/track2`, Commit `f4cba41`)

**Problem:** In `_on_shot_begin` ([bzbot.py:1323–1348](bzbot.py#L1323-L1348)) läuft die komplette Schusspfad-Simulation (max_bounces=100 × alle Obstacles) **innerhalb** von `with self._shots_lock:` — und zwar im Recv-Thread. Die 60-Hz-Schleife blockiert derweil in `_resolve_incoming_shots`/`_find_incoming_shot` auf demselben Lock → Tick-Jitter, auf dem Server bei Schuss-Bursts spürbar.
**Lösung:** Simulation VOR dem Lock ausführen (sie liest nur unveränderliche Weltdaten + lokale Schussparameter), dann kurz locken und `self._shots[...]` und `self._ricochet_paths[...]` **gemeinsam** eintragen (Atomicität von Shot+Pfad bleibt erhalten). Lock-Haltezeit sinkt von Millisekunden auf Mikrosekunden.
**Potenzial:** Kein CPU-Gewinn, aber Latenz/Jitter der 60-Hz-Schleife (mit-ursächlich für `[nr]`-Symptome unter Last). **Risiko:** Niedrig — reine Umordnung, Atomicität erhalten. **Verifikation:** tests/test_shot_parsing.py, test_hit_detection.py; manueller Lauf mit `--debug-log-tele`.

### P4 — Wahrnehmungs-Memoisierung pro Tick (LoS / Muzzle / Floor-Z) — ✅ Stufe a umgesetzt (Branch `perf/track2`, Commit `d85c19b`); Stufe b ⛔ nicht nötig (Server-Messung 2026-07-04: `_has_los_to_enemy` cum ≈0,2%/Bot im Kampf)

**Problem:** Pro 60-Hz-Tick werden identische Raycasts/Queries mehrfach mit identischen Eingaben berechnet:
- `_has_los_to_enemy(target)`: 2× in `_execute_combat_move` ([bzbot_ai.py:1460](bzbot_ai.py#L1460), [1514](bzbot_ai.py#L1514)) + 1× in `_maybe_shoot_standard` ([bzbot_ai.py:3641](bzbot_ai.py#L3641)) — bis zu 3× pro Tick.
- `_get_floor_z()`: ~3–5× pro Tick (u.a. [bzbot_ai.py:388](bzbot_ai.py#L388), [685](bzbot_ai.py#L685), [1533/1536/1544/1547](bzbot_ai.py#L1533), [bzbot.py:777](bzbot.py#L777)) — cProfile: 45.494 Calls bei 14.765 Ticks.
- `_muzzle_clear(azimuth)`: jeder Tick mit Ziel ([bzbot_ai.py:3721](bzbot_ai.py#L3721), TR: [3698](bzbot_ai.py#L3698)).

**Lösung (Stufe a, verhaltensidentisch):** Memo pro Tick — `self._tick_count` (existiert in bzbot.py) als Cache-Key: `self._memo = {}` am Tickanfang leeren (oder Key `(tick, args)`), Ergebnisse von `_has_los_to_enemy(pid)`, `_get_floor_z()`, `_muzzle_clear(az)` cachen. Innerhalb eines Ticks ändern sich `self.pos`/Gegnerpositionen nicht zwischen den Aufrufen in derselben Dispatch-Kette → **identisches Verhalten garantiert**. Vorsicht bei `_get_floor_z`: Aufrufe NACH einer `self.pos`-Mutation im selben Tick (z.B. in `_tick_jumping` nach `pos[2] +=`) dürfen nicht den alten Wert sehen → Memo-Key muss die Position enthalten: `(tick, x, y, z, own_flag)` — dann ist es ein reiner, gefahrloser Funktions-Memo.
**Lösung (Stufe b, optional, nach Messung):** TTL-Cache 0,1s (= KI-Tick) für `_has_los_to_enemy` — Staleness vergleichbar mit existierender Radar-Aufmerksamkeit (0,25s Cooldown). Nur umsetzen, wenn Stufe a + P1 nicht reichen.
**Potenzial:** LoS+FloorZ ≈ 25% der aktiven lokalen CPU → Stufe a spart grob die Hälfte davon. **Risiko:** Stufe a: null (positionsgebundener Memo-Key). Stufe b: niedrig (100ms Wahrnehmungs-Staleness). **Verifikation:** Bestehende Suite (886 Tests) muss unverändert grün sein; für Stufe a zusätzlich Assertion-Test, dass Memo-Hit == Direktberechnung.

### P5 — Ricochet-Pfad-Segmente: Zeitfenster-Suche statt Vollscan (Server-Hebel #2) — ⛔ nicht nötig (Server-Messung 2026-07-04: `_resolve_incoming_shots` + `_find_incoming_shot` ≈0,25% absolut/Bot im Kampf — nach P1 kein Hebel mehr)

**Problem:** `_resolve_incoming_shots` ([bzbot.py:645](bzbot.py#L645)) und `_find_incoming_shot` ([bzbot_ai.py:2195](bzbot_ai.py#L2195)) iterieren bei 60 Hz für jeden Ricochet-Schuss über **alle** gecachten Segmente (bei `max_bounces=100` bis zu ~100 Segmente/Schuss), obwohl pro Tick nur die 1–2 zeitlich aktuellen Segmente relevant sind. Auf Ricochet-Servern mit 10–30 aktiven Schüssen: bis zu 30 Schüsse × 100 Segmente × 60 Hz = 180k Segment-Interpolationen/s.
**Lösung:** Segmente sind zeitlich streng aufsteigend sortiert (per Konstruktion in `simulate_shot_path`). Pro Schuss einen monoton fortschreitenden Index cachen (`shot.seg_cursor`): Segmente mit `seg.t_end <= now` überspringt der Cursor einmalig; Schleife bricht ab, sobald `seg.t_start > now + Lookahead` (bei `_resolve_incoming_shots`: `now`; bei `_find_incoming_shot`: `RICO_DODGE_LOOKAHEAD`). Amortisiert O(1) statt O(Segmente) pro Tick.
**Potenzial:** Auf Rico-Servern hoch (linear in Schüssen × Segmenten), lokal moderat. **Risiko:** Niedrig — reine Suchraum-Beschneidung über bereits vorhandene Zeitschranken (`t1 <= t0`-Skips existieren schon, [bzbot.py:650](bzbot.py#L650), [bzbot_ai.py:2196](bzbot_ai.py#L2196)); Cursor-Logik mit Unit-Test absichern (Segment-Grenzfälle: exakt `t_end == now`). **Verifikation:** test_hit_detection.py + neuer Test „Cursor-Resolve == Vollscan-Resolve" über randomisierte Schusspfade.

### P6 — Pyramiden-Narrow-Phase: Flächennormalen vorberechnen — ⛔ obsolet nach P1 (Server-Messung 2026-07-04: `ray_pyramid_hit` tottime 0,12s + `_time_ray_hits_plane` 0,03s über 5 Kampf-Profile — die 468k Calls des Alt-Profils sind auf 13k gefallen)

**Problem:** `ray_pyramid_hit` ([shot_physics.py:442](bzflag/shot_physics.py#L442)) berechnet pro Aufruf (468k×) `math.cos(-angle)`/`math.sin(-angle)` neu (obwohl `BoxObstacle` gecachte `cos_a`/`sin_a` hat: `cos(-a)=cos_a`, `sin(-a)=-sin_a`) und ruft 5× `_time_ray_hits_plane`, das jedes Mal das **konstante** Kreuzprodukt der Ebenennormalen neu berechnet (1,23M Calls, Tupel-/Listen-Allokationen `pb`/`db`).
**Lösung:** Die 5 Ebenen einer Pyramide sind im lokalen Frame allein durch (hw, hd, hh) bestimmt → Normalen + Stützpunkte einmalig in `BoxObstacle.__post_init__` cachen (Muster existiert: Trig-Cache aus der ObstacleGrid-Session). `_time_ray_hits_plane` degeneriert dann zu 2 Skalarprodukten; `pb`/`db`-Listen durch skalare Variablen ersetzen.
**Potenzial:** 2–4× auf Pyramiden-Tests; HIX ist pyramidenreich. Nach P1 sinkt die Aufrufzahl bereits stark — deshalb NACH P1 messen, dann entscheiden. **Risiko:** Niedrig (pure-Function-Refactor). **Verifikation:** test_shot_physics.py + Äquivalenz-Fuzz (random Rays × random Pyramiden, alt == neu exakt).

### P7 — Sichtbarkeits-LoS pro Spieler cachen (skaliert mit Spielerzahl) — 🗄️ Schublade (Server-Messung 2026-07-04: Empfangspfad cum ≈0,8%/Bot; halbiert sich mit N1a automatisch. Erst wieder prüfen, wenn regelmäßig viele echte Spieler online sind)

**Problem:** Jedes eingehende `MsgPlayerUpdate` (30 Hz × Spieler) läuft durch `_should_update_player` → `_sees_in_window` → `_has_los_to_point`-Raycast ([bzbot_ai.py:2118→2096](bzbot_ai.py#L2118)). 10 Spieler ≈ 300 Raycasts/s zusätzlich; cProfile lokal: 35k Calls / 0,95s cum.
**Lösung:** Pro `PlayerInfo` das LoS-Ergebnis mit Kurzzeit-TTL (~0,1s) cachen (`info.los_cache = (until, result)`), analog zur bestehenden Radar-Aufmerksamkeit (`radar_blind_until`). FoV-Check bleibt exakt (billig).
**Potenzial:** Server: moderat–hoch bei vielen Spielern. **Risiko:** Niedrig — 100ms Staleness bei einem reinen Wahrnehmungs-Gate; die Radar-Attention arbeitet bereits mit 250ms. **Verifikation:** tests/test_targeting.py (Sichtbarkeits-Prädikate) unverändert; neuer Test für TTL-Ablauf.

### P8 — Ricochet-Sweep verschlanken (optional, NACH P1 neu bewerten) — ⛔ verworfen (Server-Messung 2026-07-04: `_compute_ricochet_aim` cum ≈0,8%/Bot bei nur 49 Sweeps — Verhaltensrisiko > Nutzen)

**Problem:** `_compute_ricochet_aim` ([bzbot_ai.py:3416](bzbot_ai.py#L3416)) simuliert ~80 Winkel (±45°, 1°-Raster, |az|>5°) + Teleporter-Fenster, alle 2s pro Ziel.
**Lösung (falls nach P1 noch nötig):** Zweistufiger Sweep — 3°-Raster (27 Sims), dann ±1° Verfeinerung um Treffer (≤6 Sims) ≈ 60% weniger Simulationen.
**Potenzial:** 2–3× auf dem Sweep. **Risiko:** **Mittel** — schmale Abprall-Korridore (1–2° breit) können im Grobraster durchrutschen → Bot findet gelegentlich einen Rico-Schuss nicht, den er heute findet. Das ist eine echte (milde) Verhaltensänderung. Deshalb: nur nach Messung, hinter Vergleichstest (Trefferquote alt vs. neu über randomisierte Szenarien, Abweichung dokumentieren).

### P9 — NumPy/Cython/mypyc (letzte Eskalationsstufe) — ⛔ nicht nötig (Server-Messung 2026-07-04: Compute ist nach P1 nicht mehr dominant — `sendto` ist es; s. Teil 1b)

Vom User erwähnt (Kandidaten: `ray_pyramid_hit`, `ray_box_hit`, `_time_ray_hits_plane`, `_ray_orig_box_hit`, `simulate_shot_path`). Einschätzung:
- **Erst P1/P5/P6 umsetzen** — sie reduzieren die Aufrufzahlen algorithmisch; Compile-Beschleunigung auf einen linearen Scan wäre Symptombekämpfung.
- Wenn danach noch nötig: **mypyc oder Cython auf das komplette Modul `shot_physics.py`** (reine Funktionen, keine dynamischen Tricks → idealer Kandidat, 5–20× auf der Narrow-Phase). mypyc zuerst versuchen: kompiliert die unveränderte .py-Quelle → kein zweiter Code-Pfad, Fallback = Interpreter. Docker: Multi-Stage-Build (Build-Stage kompiliert Wheel).
- **NumPy nicht empfohlen**: der Bounce-Loop ist verzweigungslastig und sequentiell (jeder Bounce hängt vom vorigen ab); Vektorisierung über die Sweep-Winkel wäre ein invasiver Umbau mit echtem Regressionsrisiko.
**Risiko:** mypyc/Cython niedrig fürs Verhalten (gleiche Quelle), mittel für Build/Deployment. **Verifikation:** komplette Suite gegen kompilierte Variante.

### Messmethodik (Voraussetzung für Erfolgskontrolle)

1. **Zusätzlich auf dem Server profilen**: `py-spy record --pid <bot-pid> --duration 120 --rate 250 -o flame_server.svg` im Docker-Host/Container (py-spy braucht `SYS_PTRACE`-Capability im Container). Karte/Settings sind lokal identisch (HIX), die lokale Hot-Path-Verteilung ist also übertragbar — die Server-Messung braucht es trotzdem: für die absoluten CPU-% auf der schwächeren Hardware, für Langzeit-Effekte (F3-Leak nach Stunden Uptime) und für Gefechtsphasen mit echten Spielern.
2. Vor/Nach jeder Maßnahme denselben 120s-Lauf auf derselben Karte mit ähnlicher Spielerlast; zusätzlich `docker stats`-CPU als Grobmetrik.
3. Die bestehende Perf-Suite (`pytest -m perf -s`) um einen `simulate_shot_path`-Benchmark auf einer echten großen Karte erweitern (analog zu den plan_path-Benchmarks), damit P1/P5/P6 CI-messbar sind.
4. **Alternative zu py-spy (bewährt am 2026-07-04):** cProfile direkt im Bot (Dump beim Prozess-Ende), Dateien aus dem Container extrahieren und lokal mit `pstats` auswerten. Wichtig bei der Auswertung: `total_tt` enthält Blocking-Waits — aktive CPU = Summe tottime OHNE `recv`/`recvfrom`/`lock.acquire`; und C-interne Kosten (z.B. getaddrinfo in `sendto`) sind für cProfile unsichtbar und landen im tottime des aufrufenden C-Calls.

### Übersichtstabelle Performance

| ID | Maßnahme | Server-Potenzial | Lokal | Risiko | Aufwand |
|----|----------|------------------|-------|--------|---------|
| P1 | Broad-Phase-Grid in `simulate_shot_path` | ⭐⭐⭐ (5–20×) | ⭐ | niedrig–mittel | mittel |
| P2 | `test_obs` vorberechnen | ⭐ | ○ | null | trivial |
| P3 | Simulation aus `_shots_lock` ziehen | ⭐⭐ (Jitter/[nr]) | ○ | niedrig | klein |
| P4a | Per-Tick-Memo (LoS/FloorZ/Muzzle) | ⭐⭐ | ⭐⭐ | null | klein |
| P4b ⛔ | LoS-TTL 0,1s | ⭐ | ⭐ | niedrig | klein |
| P5 ⛔ | Segment-Cursor statt Vollscan | ⭐⭐⭐ (Rico-Server) | ⭐ | niedrig | mittel |
| P6 ⛔ | Pyramiden-Normalen vorberechnen | ⭐⭐ | ⭐ | niedrig | mittel |
| P7 🗄️ | Sichtbarkeits-LoS-TTL pro Spieler | ⭐⭐ (viele Spieler) | ○ | niedrig | klein |
| P8 ⛔ | Sweep-Raster vergröbern | ⭐ | ⭐ | **mittel** (Verhalten) | klein |
| P9 ⛔ | mypyc/Cython shot_physics | ⭐⭐ | ⭐ | niedrig/mittel (Build) | mittel–groß |

(⛔/🗄️ = Entscheidung aus der Server-Messung 2026-07-04, Details in den Überschriften und in Teil 1b.)

---

## Teil 1b: Server-Messung 2026-07-04 — Idle-Baseline (N-Punkte)

Die in Track 2 vorgesehene Server-Messung liegt vor: cProfile-Dumps aus dem Live-Container, zwei Sessions à ~2min — **IDLE** (4 Bots, niemand sonst auf dem Server, docker stats ~20% Baseline-CPU) und **KAMPF** (User hat mitgekämpft). Aktive CPU (= total_tt minus Blocking `recv`/`recvfrom`/`lock.acquire`): IDLE 15,1%, KAMPF 16,6% pro Bot. **Ziel: Idle-Baseline < 10% ohne Verhaltensänderung.**

**Kernbefund: `sendto` dominiert beide Sessions — 46,6% (IDLE) bzw. 41,8% (KAMPF) der aktiven CPU**, ~57 Pakete/s à 1,27ms. Zwei unabhängige, verifizierte Ursachen (N1a, N1b). Alles andere ist dagegen klein: Empfangspfad der Peer-Updates ≈1%/Bot (halbiert sich mit N1a von selbst, ebenso die bzfs-Relay-Last), Idle-Nav ≈2–3%/Bot (Wander-Verhalten + einmaliger `_vn_cache`-Warm-up — bewusst NICHT anfassen, Drosselung wäre sichtbare Verhaltensänderung).

### N1a — 30-Hz-Update-Kadenz reparieren (Kadenz-Bug) ⭐ — ✅ umgesetzt (Branch `perf/idle-baseline`, Commit `72b4ee8`; `_maybe_send_server_update` mit Stall-Klemme + Kadenz-Anker vor der Schleife, tests/test_update_cadence.py)

**Problem (verifizierter Bug):** `self._next_server_update = 0.0` (bzbot.py `__init__`) wird in `_run_game_loop` gegen `time.monotonic()` (= System-Uptime, auf dem Server riesig) verglichen → die Kadenz-Prüfung ist anfangs IMMER wahr. Der Catch-up `+= _server_update_interval` holt das Start-Defizit nur mit 1s/s auf → **der Bot sendet so lange mit Tick-Rate (~57–60 Hz) statt 30 Hz, wie der Host beim Bot-Start schon lief** — auf dem Server faktisch für immer. Beweis im Profil: `_send_update`-ncalls == Tick-Anzahl. Zum Vergleich: der echte Client sendet per Dead-Reckoning nur bei Prediction-Fehler, hart gedrosselt auf `_updateThrottleRate` (Default 30) — 30 Hz ist also die obere Grenze des Spec-Konformen (`Player.cxx:isDeadReckoningWrong`; `_on_set_var` trackt `_updateThrottleRate` bereits korrekt).
**Fix:** `_next_server_update = time.monotonic()` unmittelbar vor der Schleife; Kadenz-Block als testbare Methode `_maybe_send_server_update(now)` mit Stall-Klemme (nach Stall neu aufsetzen statt Burst-Nachholen).
**Potenzial:** Paketrate −47%; zusätzlich halbieren sich Peer-Empfangslast ALLER Bots und bzfs-Relay-Last (jedes Update geht an N−1 Clients). **Risiko:** Null — stellt das dokumentierte Soll-Verhalten (30 Hz) her. **Verifikation:** tests/test_update_cadence.py (Kadenz ~30/s trotz großem monotonic-Offset; kein Burst nach Stall; `_updateThrottleRate` respektiert).

### N1b — UDP-Zieladresse einmal auflösen (DNS pro Paket) ⭐ — ✅ umgesetzt (Branch `perf/idle-baseline`, Commit `2538f5b`; `_server_addr` = getpeername()-IP, beide sendto-Stellen umgestellt, tests/test_client_udp_addr.py)

**Problem (verifiziert):** `self._udp_sock.sendto(data, (self.host, self.port))` (client.py) — bei einem Hostnamen (Docker-Servicename, auch „localhost") macht CPython **pro Paket ein getaddrinfo** im C-Code von `sendto` (für cProfile unsichtbar → landet im sendto-tottime). Lokal gemessen: 2,6µs/Paket (IP-Tupel) vs. 90,4µs (Hostname-Tupel); im Container mit Docker-Embedded-DNS (127.0.0.11) erklärt das die 1,27ms vollständig. Nebeneffekt: `sendto` läuft im Game-Loop-Thread → blockierte bisher ~7% der Wall-Time der 60-Hz-Schleife (Tick-Jitter).
**Fix:** Nach erfolgreichem TCP-Connect die tatsächliche Server-IP cachen (`self._udp_addr = (sock.getpeername()[0], self.port)` — garantiert dieselbe IP wie TCP) und beide sendto-Stellen darauf umstellen. Numerisches IP-Tupel → inet_pton-Fastpath, kein getaddrinfo. Bewusst KEIN `udp.connect()` (würde Empfangsfilterung/ICMP-Fehlersemantik ändern; als Option dokumentiert, nicht nötig).
**Potenzial:** sendto ~7,2% absolut/Bot → ~0,1%. **Risiko:** Null (gleiche Pakete, gleiches Ziel). **Verifikation:** Client-Test mit gemocktem Socket (nach `connect()` ist `_udp_addr` die getpeername-IP; sendto erhält das gecachte Tupel).

### N2 — Leerlauf-Early-Outs (offen, trivial)

`_resolve_incoming_shots`, `_cleanup_shots` und `_find_incoming_shot` laufen 60 Hz auch bei komplett leeren Shot-Dicts durch Lock/Iterations-Overhead (~0,2–0,5%/Bot im Idle). Früher `if not self._shots and not self._ricochet_paths: return` (GIL-sicherer Lesezugriff, kein Lock nötig). **Risiko:** Null. **Verifikation:** bestehende test_hit_detection.py.

### N3 — Tick-Wait verschlanken (Schublade)

`Event.wait(timeout)` pro Tick kostet ~0,4%/Bot (Condition-Objekt + Lock-Maschinerie). `time.sleep` wäre billiger, verzögert aber `stop()` um bis zu 16ms. **Nur angehen, falls die Nachmessung nach N1a/N1b immer noch >10% zeigt.**

### Erwartung Nachmessung

Rechnerisch pro Bot (IDLE): ~20% → **~8–10%** (sendto-Posten praktisch weg, eigener Sendepfad + Peer-Empfang halbiert); im neuen Profil erwartbar: sendto-Anteil <5% der aktiven CPU, ~30 `_send_update`-Calls/s. Abnahme: Redeploy, `docker stats` über ~10min Idle, optional neue cProfile-Runde.

---

## Teil 2: Fehlerpotenzial (Code-Review-Befunde)

Sortiert nach Schwere. „Verifiziert" = im Code nachvollzogen, kein Verdacht. Der Bot hat keine *bekannten* Bugs — die folgenden Punkte sind latent (treten nur unter bestimmten Bedingungen auf) oder betreffen stille Ungenauigkeiten. Die 🔴-Punkte sind zugleich Performance-relevant (F3 direkt, F2 als Crash-Ursache) und laufen deshalb im Roadmap-Track 1 **vor** dem Performance-Track.

### F1 — GM-Bedrohungsposition wird doppelt extrapoliert (verifizierter latenter Bug) 🔴 — ✅ umgesetzt (Branch `bugfix/track1`, Commit `5c3fe0f`; zusätzlich `_compute_dodge_dir` + J1a-Elapsed-Abzug GM-bereinigt)

**Befund:** Für GM-Schüsse wird `shot.pos` laufend auf die AKTUELLE Raketenposition gesetzt — durch Integration in `_resolve_incoming_shots` ([bzbot.py:624–626](bzbot.py#L624)) und durch `_on_gm_update` ([bzbot.py:1510](bzbot.py#L1510)). `Shot.position_at(t)` rechnet aber `pos + vel * (t − fire_time)` ([bzbot.py:94–98](bzbot.py#L94)) — für GM wird die **gesamte bisherige Flugzeit ein zweites Mal** aufaddiert. `_find_incoming_shot` ruft für GM-Schüsse genau dieses `position_at(now)` auf ([bzbot_ai.py:2151](bzbot_ai.py#L2151), kein GM-Sonderfall) → der Dodge sieht die Rakete bei 100 u/s nach 1s Flugzeit ~100u vor ihrer echten Position. Häufige Folge: `t_rel_raw < 0` → „entfernt sich" → **GM wird beim Ausweichen ignoriert** (oder Phantom-Dodge).
**Zusatzbefund (gleicher Komplex):** Auf Teleporter-Karten cached `_on_shot_begin` für GM einen **geraden** `simulate_shot_path` (`_tele_route` greift, GM ist nicht ausgenommen, [bzbot.py:1333–1338](bzbot.py#L1333)) → `_find_incoming_shot` überspringt den GM dann im Direkt-Zweig ([bzbot_ai.py:2149](bzbot_ai.py#L2149)) und bewertet stattdessen den falschen geraden Pfad einer gelenkten Rakete.
**Fix:** (a) In `_find_incoming_shot` für `shot.is_gm` direkt `shot.pos` verwenden (Position ist aktuell, keine Extrapolation); (b) in `_on_shot_begin` GM von `_tele_route`/Pfad-Cache ausnehmen (`and not shot.is_gm`).
**Risiko des Fixes:** Gering; Verhalten gegen GM wird korrekter (= Verhaltensänderung, aber in Richtung Spezifikation FSD-Dodge). **Test:** Unit-Test: GM 50u entfernt, vel auf Bot zu, 1s Flugzeit simuliert → `_find_incoming_shot` muss den GM liefern (schlägt heute fehl).

### F2 — `players`/`flags`-Dicts: Iteration ohne Lock über Thread-Grenze (verifiziertes Race) 🔴 — ✅ umgesetzt (Branch `bugfix/track1`, Commit `4646646`; Konvention in DEVELOPER.md §2 dokumentiert)

**Befund:** Message-Handler laufen im Recv-Thread (client `_dispatch`); `_on_add_player`/`_on_remove_player` mutieren `self.players`, `_on_flag_update`/Grab/Drop mutieren `self.flags`. Die Game-Loop (Main-Thread) iteriert gleichzeitig ohne Lock: [bzbot.py:728](bzbot.py#L728) (`_check_steamroller`), [bzbot_ai.py:466](bzbot_ai.py#L466) (`_genocide_multikill_possible`), [bzbot_ai.py:2247](bzbot_ai.py#L2247) (`_find_target_player`), analoge Stellen für `self.flags` (`_flags_on_route_all`, `_new_target`). Join/Leave eines Spielers während der Iteration → `RuntimeError: dictionary changed size during iteration` → **die Game-Loop (Main-Thread) stirbt, der Bot-Prozess crasht**. Selten (Fenster = µs, Trigger = Join/Leave), aber real — und ein plausibler Kandidat für sporadische, bisher unerklärte Bot-Abstürze unter Server-Betrieb.
**Fix (minimal-invasiv):** An allen Iterationsstellen über Thread-geteilte Dicts Snapshot ziehen: `for pid, info in list(self.players.items())`. Kein neues Lock nötig (CPython-GIL macht die Snapshot-Erstellung atomar genug). Grep-Checkliste: alle `self.players.items()/values()` und `self.flags.items()/values()`-Iterationen außerhalb des Recv-Threads.
**Risiko:** Null (Semantik identisch, Kopie ist Momentaufnahme). **Test:** deterministisch kaum testbar → Begründungskommentar an den Stellen + Konvention in DEVELOPER.md.

### F3 — `_ricochet_paths`-Leak bei Timeout-Cleanup (verifiziert; frisst Server-CPU über Uptime) 🔴 — ✅ umgesetzt (Branch `bugfix/track1`, Commit `10ba41e`)

**Befund:** `_cleanup_shots` ([bzbot.py:2045–2049](bzbot.py#L2045)) entfernt abgelaufene Schüsse nur aus `self._shots`, NICHT aus `self._ricochet_paths`. `_resolve_incoming_shots` räumt zwar beide — läuft aber nur solange `self.alive`. Während Tod/Explosion/Respawn (≥3–8s, häufig!) ablaufende Rico-/Tele-Schüsse hinterlassen verwaiste Segmentlisten (bis ~100 Segmente bei `max_bounces=100`). Diese werden erst überschrieben, wenn dieselbe `(shooter, shot_id)`-Kombination wiederkehrt — Shot-IDs wachsen aber (16-Bit-Raum). `_find_incoming_shot` iteriert bei jedem Threat-Check über **alle** Einträge ([bzbot_ai.py:2187](bzbot_ai.py#L2187)) → Speicher UND CPU wachsen mit der Laufzeit. Bots auf dem Server laufen stundenlang → schleichende CPU-Degradation, passt zum Symptom „Server 30–50%, lokal 1%".
**Fix (2 Zeilen):** In `_cleanup_shots` im selben Durchlauf `self._ricochet_paths.pop(k, None)`. **Risiko:** Null. **Test:** Unit-Test: Schuss einfügen + Pfad cachen, `is_expired` erzwingen, `_cleanup_shots` → beide Dicts leer.

### F4 — Ungeklemmtes `dt_r`: Tunneling & Geister-Durchflüge bei Stalls 🟡 — ✅ umgesetzt (Branch `bugfix/track1`, Commit `5d1597d`; inkl. optionaler Segment-Anfang-Skip-Härtung. Nachtrag 2026-07-04, Branch `fix/sb-shots`: Punkt 3 überholt — Hit-Detection prüft jetzt das echte Fenster `min(_last_hit_check_t, now−dt)` mit Relativ-Sweep der Eigenbewegung; der 0,1s-Clamp gilt nur noch für Bewegung/GM-Steering)

**Befund:** [bzbot.py:507](bzbot.py#L507) `dt_r = now - last_tick` ohne Obergrenze. Bei Stalls (GC, Async-Plan-Übernahme, Netz-Hänger, Container-Scheduling — auf dem Server wahrscheinlicher als lokal) entsteht ein großer Einzelschritt mit drei Folgeproblemen:
1. `pos += vel * dt` springt viele Einheiten → `_apply_obstacle_bounds` prüft nur den Zielpunkt → Tunneln durch dünne Wände.
2. GM-Steering dreht `max_turn = _gm_turn_angle * dt` übergroß ([bzbot.py:614](bzbot.py#L614)).
3. Hit-Detection: `prev_t = max(fire_time, now − min(dt, 0.2))` ([bzbot.py:631](bzbot.py#L631)) — Flugzeit jenseits 0,2s wird **nie** geprüft; zusätzlich kann der Endpunkt-basierte „entfernt sich"-Skip ([bzbot.py:677](bzbot.py#L677)) ein langes Segment überspringen, das den Tank DURCHquert hat → Schuss trifft nie („Geister-Durchflug").
**Fix:** Zentral in `_run_game_loop`: `dt_r = min(dt_r, 0.1)` (6 Nominal-Ticks) mit Kommentar; optional den Skip in (3) nur anwenden, wenn auch der Segment-**Anfang** sich schon entfernt (`vel·(a−tank) > 0`).
**Risiko:** Gering — ändert nur das Verhalten in pathologischen Stall-Fällen, und dort zum Besseren (Zeit „verlangsamt" statt Sprung). Der 0,1s-Wert sollte bewusst gewählt/abgestimmt werden. **Test:** Unit-Test `_resolve_incoming_shots` mit dt=0,5s-Segment quer durch den Tank → muss treffen.

### F5 — `SHOT_RANGE`-Konstante statt Server-Variable an Entscheidungsstellen 🟡 — ✅ umgesetzt (Branch `consistency/track3`, Commit `ef34c67`; bewusst `self._shot_range` ohne Flaggen-Multiplikatoren, Begründung im Code)

**Befund:** `self._shot_range` wird via `MsgSetVar _shotRange` nachgeführt und `_effective_shot_range()` existiert ([bzbot_ai.py:501](bzbot_ai.py#L501)) — aber drei Entscheidungsstellen nutzen die Konstante 350: [bzbot_ai.py:1298](bzbot_ai.py#L1298) (Threat-Radius), [1461](bzbot_ai.py#L1461) (`_dist_thresh` im COMBAT-Direktmodus), [2257](bzbot_ai.py#L2257) (Zielwahl-Fenster). Auf Servern mit abweichender `_shotRange`/`_shotSpeed` kämpft der Bot mit falschen Distanzannahmen.
**Fix:** `_effective_shot_range()` verwenden (bei 1298/2257 ggf. bewusst OHNE Flaggen-Multiplikatoren → dann `self._shot_range`; Entscheidung je Stelle dokumentieren). **Risiko:** Niedrig auf Standard-Servern (Werte identisch), gewollte Korrektur auf Custom-Servern. **Test:** bestehende Targeting-Tests + einer mit verändertem `_shotRange`.

### F6 — Radar-Reichweite: bewusst halbe Weltgröße, aber `_worldSize` nachführen 🟡 — ✅ umgesetzt (Branch `consistency/track3`, Commit `67b3319`; Design-Begründung im Docstring)

**Befund:** `RADAR_RANGE = WORLD_HALF_DEFAULT` (= 400, [bzbot_ai.py:165](bzbot_ai.py#L165)) ist hart kodiert; `_effective_radar_range()` ([bzbot_ai.py:2231](bzbot_ai.py#L2231)) skaliert nur den BU-Fall. `self.world_half` wird via `_worldSize` nachgeführt ([bzbot.py:1688](bzbot.py#L1688)) — die Radar-Reichweite bleibt aber bei 400, auch wenn die Weltgröße abweicht.
**Klärung (User): Die Begrenzung auf die HALBE Weltgröße ist gewollt** (Fairness-Limit): Das echte Client-Radar hat mehrere Zoomstufen und dreht sich mit der Blickrichtung — ein menschlicher Spieler sieht auf Standard-Zoom je nach Blickwinkel nie permanent die ganze Karte. Da der Bot über FoV+LoS bereits die ganze Karte einsehen darf, würde ein Radar über die volle Weltgröße ihn faktisch allwissend machen. Die Reichweite soll also NICHT auf die volle Weltgröße angehoben werden — sie soll aber Änderungen der Server-Variable `_worldSize` folgen und dann weiterhin die jeweilige **halbe** Weltgröße betragen.
**Fix:** `_effective_radar_range()` auf `self.world_half` stützen (= halbe Weltgröße, via `_worldSize` nachgeführt; BU-Viertelung beibehalten). Die Design-Entscheidung „halbe, nicht ganze Weltgröße" als Kommentar dort + in DEVELOPER.md dokumentieren, damit künftige Reviews sie nicht erneut als Bug aufgreifen. **Risiko:** Null auf Standardkarten (world_half = 400 = heutiger Wert); gewollte Korrektur bei abweichender Weltgröße. **Test:** Unit-Test mit `world_half = 800` (1600u-Welt) → Gegner bei 600u wird gefunden, bei 900u nicht.

### F7 — `TANK_HEIGHT`-Konstante vs. `self._tank_height` gemischt 🟡 — ✅ umgesetzt (Branch `consistency/track3`, Commit `cc5f2a0`; TANK_WIDTH/RADIUS-Kette bewusst NICHT umgestellt: daran hängen Modulkonstanten (HIT_RADIUS/DODGE_DIST/MUZZLE_FRONT) und der NavGrid-GRID_PAD — Umstellung wäre ein eigener Punkt mit Pad-Neuberechnung beim Weltladen)

**Befund:** Dieselbe physikalische Größe wird mal als Konstante (22 Vorkommen), mal als Server-nachgeführtes Attribut (15 Vorkommen, `_tankHeight`-Handler existiert [bzbot.py:1979](bzbot.py#L1979)) verwendet — teils in derselben Funktion: `_resolve_incoming_shots` nutzt für die eigene Tankmitte `self._tank_height` ([bzbot.py:569](bzbot.py#L569)), für das GM-Fremdziel `TANK_HEIGHT` ([bzbot.py:601](bzbot.py#L601)); ebenso `_execute_combat_move` ([bzbot_ai.py:1462](bzbot_ai.py#L1462)), LoS-Augenhöhen (`TANK_HEIGHT * 0.5` in `_has_los_to_point`/`_sees_in_window`), u.v.m. Auf Servern mit verändertem `_tankHeight` rechnen verschiedene Codepfade mit verschiedenen Höhen.
**Fix:** Mechanische Vereinheitlichung auf `self._tank_height` (gilt serverweit für alle Tanks). Grep-Checkliste `TANK_HEIGHT` außerhalb von constants/Defaults. **Risiko:** Null auf Standard-Servern (2.05 == 2.05); Konsistenzgewinn auf Custom-Servern. Gleiches Muster einmalig für `TANK_WIDTH`/`TANK_LENGTH`/`TANK_RADIUS`-Vorkommen prüfen.

### F8 — Kleinere Ungenauigkeiten / Beobachtungen 🟢 — ✅ Feuer-Gate umgesetzt + Doku-Punkte (Branch `consistency/track3`, Commit `2953cd5`: `_fire_gate_rad` 25°→5° linear 10u–100u; GM-Selbsttreffer-Skip kommentiert; Radar-Attention-Kommentar korrigiert. Offen/bewusst belassen: Steamroller-BU-Großzügigkeit + fehlende `_srRadiusMult`-Nachführung → W3, `_vn_cache`-Hinweis)

- **Steamroller-BU-Fall zu großzügig:** [bzbot.py:726–739](bzbot.py#L726): JEDER Gegner überrollt einen BU-Bot mit SR-Radius `TANK_RADIUS*(1+SR_RADIUS_MULT)`; real crusht ein normaler Tank einen eingegrabenen nur bei echter Überlappung. `_srRadiusMult` wird zudem nicht via MsgSetVar nachgeführt. Nebenbei: `math.sqrt(math.hypot(dx,dy)**2 + …)` — redundantes sqrt(hypot²).
- **Eigener GM kann Selbsttreffer nicht auslösen:** [bzbot.py:590](bzbot.py#L590) skippt eigene GM-Schüsse — im echten BZFlag kann die eigene Rakete einen treffen. **Klärung (User): bewusste Vereinfachung** — der Fall ist praktisch nur mit stark veränderten Server-Variablen oder extrem ungünstigen Teleporter-Schüssen erreichbar und wurde bewusst ignoriert. Nur als Kommentar an der Stelle dokumentieren, keine Logik-Änderung.
- **25°-Feuer-Gate:** `_maybe_shoot_standard` feuert entlang `self.azimuth`, erlaubt aber bis 25° Abweichung vom (Rico-)Zielwinkel ([bzbot_ai.py:3669](bzbot_ai.py#L3669), [3689](bzbot_ai.py#L3689)). Für Direktschüsse auf kurze Distanz plausible Streuung; auf große Distanz (und in Ricochet-/Tor-Korridoren) ist ein 20°-Fehlschuss praktisch garantiert → Slot-Verschwendung. **Entscheidung (User): distanzabhängiges Gate** statt festem 25°: linear von 25° (Zieldistanz ≤10u) auf 5° (≥100u) verengen, z.B. `max_dev_deg = 25 − 20 · clamp((dist − 10) / 90, 0, 1)`; gilt einheitlich für Direkt- wie Indirekt-Schüsse. Milde, gewollte Verhaltensänderung (weniger Fehlschüsse auf Distanz) → eigener kleiner PR im Konsistenz-Track, Smoke-Test auf Trefferquote.
- **Radar-Attention ist Message-Raten-abhängig:** `_should_update_player` würfelt pro eingehendem Update (30 Hz × Spieler), Kommentar sagt „pro Tick" ([bzbot_ai.py:2121](bzbot_ai.py#L2121)) — bei anderer Server-Update-Rate verschiebt sich die effektive Aufmerksamkeit. Dokumentieren oder auf Zeitbasis normieren.
- **Wahrnehmungs-Arbeit im Recv-Thread:** `_on_player_update_full` → `_sees_in_window` → Raycasts laufen im Recv-Thread und lesen `self.pos` ohne Lock (torn reads theoretisch möglich, praktisch durch GIL+float-Zuweisung mild). Mit P7 (TTL-Cache) sinkt die Last; Konvention dokumentieren.
- **`_vn_cache` unbounded:** Sprungkanten-Cache wächst pro (Knoten × tank_speed-Variante); auf HIX-Größe unkritisch (~41k × wenige Speeds), aber bewusst so — Kommentar existiert, ggf. Obergrenze notieren.

### F9 — Duplizierte Berechnungen zentralisieren (Divergenz-Prävention) — ✅ umgesetzt (Branch `consistency/track3`, Commit `036b43c`: `_hitbox_half_dims`, `_instant_shot_hits` (Laser/Thief vereint), `_turn_toward` (5 Stellen), `_own_flag_bytes` (6 Stellen); Varianten mit effektiver Drehrate/60°-Cap bewusst eigenständig)

| Duplikat | Stellen | Helper-Vorschlag |
|---|---|---|
| Hitbox-Half-Dims (O/T/TH-Skalierung + N-Sonderfall + Schussradius) | [bzbot.py:632–639](bzbot.py#L632), [1378–1385](bzbot.py#L1378), [1416–1423](bzbot.py#L1416) | `_hitbox_half_dims() -> (half_len, half_w, half_h)` |
| Flag-Bytes-Encoding `(own_flag.encode('ascii')+b'\x00\x00')[:2]` | [bzbot_ai.py:3364](bzbot_ai.py#L3364), [3405](bzbot_ai.py#L3405), [3643](bzbot_ai.py#L3643) | `_own_flag_bytes() -> bytes` |
| Dreh-Snippet (max_turn/diff/ang_vel/copysign) | [bzbot_ai.py:727–735](bzbot_ai.py#L727), [1524–1529](bzbot_ai.py#L1524), `_navigate_wp`, `_tick_nav_*` | `_turn_toward(target_az, dt) -> diff` |
| Laser-/Thief-Segment-Sofortcheck (nahezu identische 25-Zeilen-Blöcke) | [bzbot.py:1374–1411](bzbot.py#L1374) vs. [1412–1458](bzbot.py#L1412) | gemeinsame Funktion mit Callback/Flag |

Jede Zentralisierung einzeln + Testlauf; das sind die Stellen, an denen künftige Flaggen-/Regel-Änderungen sonst nur „an einer von drei Stellen" landen. **Hinweis zur Reihenfolge:** F9 VOR dem Mixin-Split (Teil 3, W4/W5) umsetzen — dann wandern die Helper beim Split als eine Einheit mit, statt dass die Duplikate auf mehrere neue Module verteilt werden.

---

## Teil 3: Wartbarkeit

### Wechselwirkungen mit Teil 1 (Performance) und Teil 2 (Fehlerpotenzial) — zuerst lesen

Die Umstrukturierung verschiebt Code, auf den sich alle Datei:Zeile-Referenzen aus Teil 1 und Teil 2 beziehen. Damit die Übergabe an Junior/Sonnet nicht ins Leere läuft, gelten folgende Regeln:

1. **Der Struktur-Track läuft ZULETZT** (Roadmap-Track 4) — erst wenn alle P- und F-Maßnahmen umgesetzt oder bewusst verworfen sind. Dann gibt es keine offenen Punkte mehr, deren Referenzen der Umbau entwerten könnte.
2. **Einzige Ausnahme: W6** (ObstacleGrid-Split) ist Voraussetzung für P1 und läuft deshalb **im Performance-Track** — ein kleiner, isolierter Datei-Split ohne Auswirkung auf bzbot.py/bzbot_ai.py-Referenzen.
3. **Methodennamen sind die stabilen Anker:** Kein Schritt dieses Plans benennt Methoden oder Attribute um. Wird ein P/F-Punkt doch erst NACH dem Split umgesetzt, findet man die Stelle per Grep auf den im Punkt genannten Methodennamen; die Zeilennummern sind dann hinfällig, der Befundtext bleibt gültig.
4. **Falls der Struktur-Track vorgezogen werden muss:** Jeder W-PR aktualisiert verpflichtend die Datei-Referenzen der noch offenen P/F-Punkte in diesem Plan (reine Pfad-Ersetzung anhand der Tabelle unten).
5. **F9 (Duplikat-Helper) gehört VOR den Split** (Track 3 vor Track 4): Die Helper entstehen an einer Stelle und wandern beim Split als Einheit mit — andernfalls verteilt der Split die Duplikate auf mehrere neue Module und F9 wird teurer.

**Landkarte: Wo landen die P/F-Baustellen nach dem Split?** (nur relevant, falls ein Punkt beim Umbau noch offen ist)

| P/F-Punkt | Heutige Stelle (Methode) | Modul nach dem Split |
|---|---|---|
| P1, P2, P6, P9 | `simulate_shot_path`, `ray_*_hit`, `_time_ray_hits_plane` | `bzflag/shot_physics.py` (unverändert) + `bzflag/obstacle_grid.py` (W6) |
| P3, F1(b) | `_on_shot_begin` | `bot/handlers.py` |
| P4a | `_has_los_to_enemy`/`_muzzle_clear`/`_get_floor_z` (Memo-Reset im Tick-Start) | `bot/ai/perception.py` + `bot/ai/physics.py` (Reset: `bot/core.py`) |
| P5 | `_resolve_incoming_shots` + `_find_incoming_shot` | `bot/hit_detection.py` + `bot/ai/perception.py` |
| P7, F8-Radar-Attention | `_should_update_player`, `_sees_in_window` | `bot/ai/perception.py` |
| P8 | `_compute_ricochet_aim` | `bot/ai/shooting.py` |
| F1(a) | `_find_incoming_shot`, `Shot.position_at` | `bot/ai/perception.py` + `bot/models.py` |
| F2 | Dict-Iterationen (Grep: `players.items`, `flags.items`) | verteilt — deshalb VOR dem Split fixen |
| F3 | `_cleanup_shots` | `bot/hit_detection.py` |
| F4 | `_run_game_loop`, `_resolve_incoming_shots` | `bot/core.py` + `bot/hit_detection.py` |
| F5–F7 | verstreut (Grep: `SHOT_RANGE`, `TANK_HEIGHT`; F6: `_effective_radar_range`) | verteilt — deshalb VOR dem Split fixen (Track 3) |
| F9 | Duplikat-Tabelle in Teil 2 | Helper landen in `bot/hit_detection.py` / `bot/ai/shooting.py` / `bot/util.py` |

Umgekehrt profitiert die Wartbarkeit von Teil 1/2: W3 (tabellen-getriebenes `_on_set_var`) ist der natürliche Ort, die in F8 notierte fehlende `_srRadiusMult`-Nachführung zu ergänzen; W8 (Server-Var-Tabelle in constants.py) ist die Dauerlösung für die F5–F7-Fehlerklasse „Konstante statt Server-Variable"; P1 definiert den Schnitt für W6.

### Ist-Zustand (gemessen)

| Datei | Zeilen | Inhalt |
|---|---|---|
| `bzbot_ai.py` | 3.797 | ~200 Zeilen Konstanten + `AIState` + `BZBotAI`-Mixin mit ~120 Methoden (Physik, State-Machine, Combat, Navigation, Wahrnehmung, Targeting, Schießen — alles in einer Klasse) |
| `bzbot.py` | 2.346 | `Shot`/`PlayerInfo`/`FlagInfo`-Dataclasses, `BZBot(BZBotAI)` (300-Zeilen-`__init__`, Game-Loop, Hit-Detection, ~25 `_on_*`-Message-Handler), CLI (`parse_args`/`main`), Debug-Dump, 90-Konstanten-Re-Export |
| `bzflag/` | 3.642 | Bereits sauber getrennt: protocol, client, world_map, world_parser, shot_physics, nav_graph |
| `bot_manager.py` | 857 | Config, ServerObserver, BotProcess, BotManager — in Ordnung, bleibt |

Auffällige Einzelmethoden (Kandidaten für interne Aufteilung, unabhängig vom Datei-Split):
- `_on_set_var` ([bzbot.py:1674–2019](bzbot.py#L1674)): **345 Zeilen** if/elif-Kette für Server-Variablen.
- `BZBot.__init__` ([bzbot.py:160–462](bzbot.py#L160)): ~300 Zeilen Attribut-Initialisierung (~150 Attribute).
- `_on_shot_begin` ([bzbot.py:1300–1465](bzbot.py#L1300)): 165 Zeilen mit 3× dupliziertem Hitbox-Skalierungsblock (Detail in Teil 2, F9).
- `_new_target` (136 Z.), `_execute_combat_move` (140 Z.), `_check_tactical_jump` (115 Z.), `_compute_aim_point` (~97 Z.).

195 Import-Stellen in 15 Testdateien hängen an `bzbot`/`bzbot_ai` → jeder Umbau braucht **Kompatibilitäts-Re-Exports**, sonst bricht die Suite (886 Tests).

### Ziel-Struktur (Vorschlag)

Grundprinzip: `BZBotAI` ist bereits ein Mixin ohne eigenen State (alle Attribute entstehen in `BZBot.__init__`) → die Zerlegung in **mehrere Mixins in separaten Dateien** ist eine rein mechanische Code-Verschiebung ohne Verhaltensänderung. Die `bzflag/`-Schicht (Protokoll/Engine) bleibt wie sie ist — sie ist schon gut geschnitten.

```
bzflag-bot/
├── bzbot.py                      # NUR noch: Shebang, parse_args, main, Managed-stdin-Reader,
│                                 # Kompat-Re-Exports (from bot.constants import *, from bot.core import BZBot, …)
├── bot_manager.py                # unverändert (startet weiterhin `python bzbot.py …` als Subprozess)
├── bzflag/                       # Engine-Schicht (unverändert bis auf einen Split)
│   ├── protocol.py               #   Wire-Format, Message-Codes, pack/unpack
│   ├── client.py                 #   TCP/UDP-Transport, Handshake, Dispatch
│   ├── world_map.py              #   BoxObstacle, TeleporterObstacle, WorldMap
│   ├── world_parser.py           #   Binär-Parser
│   ├── obstacle_grid.py          #   NEU (W6, läuft im Performance-Track als P1-Voraussetzung):
│   │                             #   ObstacleGrid aus nav_graph.py herausgelöst
│   │                             #   (Grund: P1 braucht das Grid in shot_physics; direkter Import
│   │                             #    nav_graph→shot_physics existiert schon — Gegenrichtung wäre ein Zyklus)
│   ├── shot_physics.py           #   Ray-Tests, simulate_shot_path (+ P1-Grid-Parameter)
│   └── nav_graph.py              #   FloorLayer, NavGraph, A*, Sprungkanten (re-exportiert ObstacleGrid)
└── bot/                          # NEU: die eigentliche Bot-Logik als Paket
    ├── __init__.py
    ├── constants.py              # ALLE Spiel-Konstanten (heute bzbot_ai.py:27–225) — „Header-Datei".
    │                             # Gliederung nach Blöcken: Tank-Physik / Schuss / Flaggen-Defaults /
    │                             # Timing+Raten / Navigation+A* / Wahrnehmung / Kill-Reasons.
    │                             # Pro Konstante im Kommentar: zugehörige Server-Variable (_tankSpeed, …)
    │                             # falls via MsgSetVar überschreibbar → EINE nachschlagbare Tabelle.
    ├── util.py                   # _angle_diff, _wrap, _segment_point_dist3d (bzbot.py:2061)
    ├── models.py                 # Shot, PlayerInfo, FlagInfo (aus bzbot.py), AIState (aus bzbot_ai.py)
    ├── core.py                   # class BZBot(…Mixins…): __init__ (in benannte _init_*-Blöcke gegliedert),
    │                             # start/stop, _run_game_loop, _send_update, _spawn, _emit_status,
    │                             # _sync_nav_physics, Callsign-Verwaltung
    ├── handlers.py               # HandlersMixin: alle _on_* Message-Handler + _on_set_var
    │                             # (_on_set_var dabei tabellen-getrieben refactoren, s.u. W3)
    ├── hit_detection.py          # HitDetectionMixin: _resolve_incoming_shots, _report_killed,
    │                             # _report_steamrolled, _check_steamroller, _cleanup_shots
    └── ai/
        ├── __init__.py
        ├── capabilities.py       # CapabilityMixin: _can_*, _effective_*, _travel_tank_speed,
        │                         # _next_slot_ready, _apply_movement_caps, _has_presence
        ├── physics.py            # PhysicsMixin: _run_physics, _get_floor_z, _is_landed,
        │                         # _apply_obstacle_bounds, _apply_bounds, _is_inside_obstacle
        ├── states.py             # StateMachineMixin: _transition_to, _ground_state, _update_movement,
        │                         # _dispatch_movement, alle _tick_* (jumping/falling/nav_jump/
        │                         # nav_tele/z_attack/committed/landing_shot/explosion/idle/seeking)
        ├── combat.py             # CombatMixin: _tick_combat, _execute_combat_move, _combat_escalate,
        │                         # _handle_threat, _compute_dodge_dir, _setup_dodge, _pick_reposition_point
        ├── navigation.py         # NavigationMixin: _plan_path, _apply_planned_path, _submit_async_plan,
        │                         # _poll_async_plan, _trim_traversed_prefix, _navigate_wp, _advance_path,
        │                         # _wp_reach_radius, _nav_jump_*, _initiate_nav_jump, _try_engage_nav_tele,
        │                         # _check_teleport_crossing, _resync_path_after_teleport, _move_to_target
        ├── perception.py         # PerceptionMixin: _effective_fov, _in_fov, _is_ahead, _enemy_visible_*,
        │                         # _sees_in_window, _shot_visible_*, _shot_reveals_shooter,
        │                         # _should_update_player, _segment_clear, _steep_wall_ahead,
        │                         # _has_los_to_point, _has_los_to_enemy, _muzzle_clear, _find_incoming_shot
        ├── targeting.py          # TargetingMixin: _find_target_player, _get_enemy_pos, _new_target,
        │                         # _validate_and_find_target, _dist_to_target, _flags_on_route_all,
        │                         # _check_opportunistic_grab, _try_grab_flag, _is_foe, _threat_unseen
        ├── tactics.py            # TacticsMixin: _check_tactical_jump, _check_z_attack_jump,
        │                         # _z_attack_feasible, _execute_jump, _jump_launch_vz,
        │                         # _check_advance_path, _should_reverse_to_wp
        └── shooting.py           # ShootingMixin: _maybe_shoot + alle _maybe_shoot_*, _compute_aim_point,
                                  # _shot_quality, _find_ricochet_aim_angle, _compute_ricochet_aim,
                                  # _teleporter_shot_available, _indirect_shot_available,
                                  # _update_indirect_hold, _send_shot, _send_gm_update-Zulieferer
```

`bzbot_ai.py` bleibt übergangsweise als reiner Kompat-Shim bestehen (`from bot.constants import *; from bot.models import AIState; from bot.ai import BZBotAI; …`), bis die Tests migriert sind; danach löschen.
`BZBotAI` selbst bleibt als Sammelklasse erhalten: `class BZBotAI(CapabilityMixin, PhysicsMixin, StateMachineMixin, CombatMixin, NavigationMixin, PerceptionMixin, TargetingMixin, TacticsMixin, ShootingMixin)` in `bot/ai/__init__.py` — dadurch ändert sich für `BZBot` und alle Tests **nichts** an der MRO-Sichtbarkeit der Methoden.

### Migrations-Reihenfolge (jeder Schritt einzeln testbar, Suite muss nach jedem Schritt grün sein)

Startpunkt: erst nach Abschluss der Roadmap-Tracks 1–3 (s. „Wechselwirkungen" oben); nur W6 läuft vorab im Performance-Track.

1. **W1 — Konstanten extrahieren:** `bot/constants.py` anlegen, Konstanten-Block aus `bzbot_ai.py:27–225` verschieben; `bzbot_ai.py` importiert sie zurück (`from bot.constants import *` + explizites `__all__` in constants.py). Der bestehende Re-Export in `bzbot.py:42–65` bleibt unverändert funktionsfähig. Risiko: minimal. *(Hinweis: `_TINY_FACTOR`/`_NARROW_HW` beginnen mit Unterstrich → von `import *` ausgeschlossen — entweder in `__all__` explizit aufnehmen oder umbenennen in `TINY_FACTOR`/`NARROW_HW` mit Alias.)*
2. **W2 — Modelle & Utils:** `Shot`, `PlayerInfo`, `FlagInfo` (bzbot.py:73–156), `AIState`, `_angle_diff`, `_wrap`, `_segment_point_dist3d` nach `bot/models.py`/`bot/util.py`; Re-Import an alter Stelle.
3. **W3 — `_on_set_var` tabellen-getrieben:** Die 345-Zeilen-Kette durch eine Dispatch-Tabelle ersetzen: `_SETVAR_HANDLERS: dict[bytes, Callable[[BZBot, float|str], None]]` bzw. für die Mehrheit der Fälle (Float → Attribut) eine Mapping-Tabelle `var_name → (attr_name, cast, optionaler Nachlauf-Hook)`. Sonderfälle (z.B. `_jumpVelocity` → `set_physics`, Flag-Listen) bleiben eigene kleine Funktionen. Das ist der einzige Schritt in Teil 3 mit echter Code-Umformung → eigener Test, der alle heute behandelten Variablen einmal durchschickt und die resultierenden Attribute mit dem Alt-Verhalten vergleicht (Snapshot-Test VOR dem Refactor schreiben!). Dabei die in Teil 2/F8 notierte fehlende `_srRadiusMult`-Nachführung ergänzen.
4. **W4 — `BZBotAI` in Mixins zerlegen:** pro Commit EIN Mixin-Modul herauslösen (Methoden unverändert verschieben, Imports nachziehen). Empfohlene Reihenfolge (nach Kopplungsgrad aufsteigend): capabilities → physics → perception → shooting → targeting → tactics → navigation → combat → states.
5. **W5 — `bzbot.py` zerlegen:** handlers.py, hit_detection.py, core.py; `bzbot.py` wird dünner Entry-Point + Kompat-Re-Export. `BZBOT_SCRIPT` in [bot_manager.py:41](bot_manager.py#L41) zeigt weiterhin auf `bzbot.py` → Manager unverändert.
6. **W6 — `ObstacleGrid` → `bzflag/obstacle_grid.py`** (Voraussetzung für P1; läuft als Teil des Performance-Tracks VOR allen anderen W-Schritten). `nav_graph.py` re-exportiert (`from .obstacle_grid import ObstacleGrid`), damit bestehende Importe/Tests weiterlaufen. — ✅ umgesetzt (Branch `perf/track2`, Commit `c6818b1`; inkl. `TANK_HALF_WIDTH` als kanonische Definition dort)
7. **W7 — `__init__` gliedern:** Die 300 Zeilen in benannte private Methoden gruppieren (`_init_network()`, `_init_tank_state()`, `_init_ai_state()`, `_init_flags()`, `_init_nav()`, `_init_debug()`), aufgerufen aus `__init__` in fester Reihenfolge. Reine Umsortierung, keine Umbenennung von Attributen.

### Weitere Architektur-Empfehlungen (geringerer Dringlichkeit)

- **W8 — Konstanten vs. Server-Variablen dokumentieren:** In `constants.py` pro überschreibbarer Konstante die Server-Var + das Instanz-Attribut nennen (z.B. `RELOAD_TIME_DEFAULT ↔ _reloadTime ↔ self._reload_time`). Das ist die Wartbarkeits-Flanke der Fehlerklasse „Konstante statt Server-Variable benutzt" (Teil 2, F5–F7) — die Tabelle verhindert, dass diese Klasse nach den Einzelfixes wieder einreißt. Bewusste Abweichungen wie die Radar-Reichweite (F6: absichtlich halbe statt ganze Weltgröße) dort ebenfalls als solche markieren.
- **W9 — Duplizierte Berechnungen zentralisieren** (Detail in Teil 2, F9): Hitbox-Half-Dims (3×), OBB-Treffertest-Vorbereitung, Flag-Bytes-Encoding `(flag.encode+b'\x00\x00')[:2]` (3× in bzbot_ai.py) → je eine Helper-Methode. Läuft als F9 im Konsistenz-Track (Track 3), also VOR dem Struktur-Track — s. „Wechselwirkungen".
- **W10 — Docstring-Konvention beibehalten:** Der Code hat ungewöhnlich gute erklärende Docstrings/Kommentare (Warum-Ebene, BZFlag-Quellcode-Referenzen). Bei der Migration NICHT kürzen — sie sind das eigentliche Wissensarchiv des Projekts. DEVELOPER.md nach dem Split um eine Modul-Landkarte ergänzen.
- **W11 — Nicht empfohlen:** Attribute in Sub-Objekte/Dataclasses gruppieren (`self.nav.goal` statt `self._nav_goal`) oder die Mixins zu Kompositions-Objekten umbauen. Das wäre zwar „sauberer", ändert aber hunderte Zugriffsstellen → hohes Regressionsrisiko bei geringem Nutzen. Erst sinnvoll, falls das Projekt stark wächst.

### Übersichtstabelle Wartbarkeit

| ID | Maßnahme | Nutzen | Risiko | Aufwand | Wann |
|----|----------|--------|--------|---------|------|
| W1 | constants.py („Header") | ⭐⭐⭐ | minimal | klein | Track 4 |
| W2 | models.py / util.py | ⭐⭐ | minimal | klein | Track 4 |
| W3 | `_on_set_var` Tabellen-Dispatch | ⭐⭐⭐ | niedrig (mit Snapshot-Test) | mittel | Track 4 |
| W4 | BZBotAI → 9 Mixin-Module | ⭐⭐⭐ | niedrig (mechanisch, pro Commit ein Modul) | mittel–groß | Track 4 |
| W5 | bzbot.py → Entry + 3 Module | ⭐⭐⭐ | niedrig | mittel | Track 4 |
| W6 | obstacle_grid.py Split | ⭐⭐ (P1-Voraussetzung) | minimal | klein | **Track 2 (vorgezogen)** |
| W7 | `__init__` in _init_*-Blöcke | ⭐⭐ | minimal | klein | Track 4 |
| W8 | Server-Var-Tabelle in constants.py | ⭐⭐ | null | klein | Track 4 (mit W1) |
| W9 | Dup-Berechnungen → Helper | ⭐⭐ | niedrig | klein | **Track 3 (= F9)** |

---

## Empfohlene Gesamt-Reihenfolge (Umsetzungs-Roadmap)

Die Tracks spiegeln die Themen-Priorität (Performance → Fehlerpotenzial → Wartbarkeit) und laufen **sequenziell**: Der Struktur-Track kommt bewusst ganz ans Ende, damit die Datei:Zeile-Referenzen und Review-Befunde dieses Plans bis dahin gültig bleiben (s. Teil 3, „Wechselwirkungen"). Für Junior/Sonnet-Umsetzung: **ein Punkt = ein Branch/PR**, Suite (886 Tests) nach jedem Punkt grün.

1. **Track 1 — Sofort-Bugfixes (klein, hohes Server-Potenzial; F3 ist faktisch auch eine Performance-Maßnahme):** F3 (Rico-Leak, 2 Zeilen) → F2 (Dict-Snapshots) → F1 (GM-Dodge) → F4 (dt-Clamp).
2. **Track 2 — Performance:** P2 → P3 → P4a → W6 (ObstacleGrid-Split, aus dem Struktur-Track vorgezogen) → P1 (Grid in simulate_shot_path) → **Server-Messung** → danach je nach Befund P5, P6, P7, P4b, P8, P9. — ✅ P2/P3/P4a/W6/P1 umgesetzt (Branch `perf/track2`); ✅ Server-Messung 2026-07-04 erfolgt (cProfile IDLE + KAMPF aus dem Live-Container) → Ergebnis: P4b/P5/P6/P8/P9 ⛔, P7 🗄️ — stattdessen **Track N (Teil 1b)**.
2b. **Track N — Idle-Baseline (aus der Server-Messung, Teil 1b):** N1a (30-Hz-Kadenz-Fix) → N1b (UDP-Adress-Cache) → Nachmessung (User; Ziel <10% Idle) → danach ggf. N2 (Leerlauf-Early-Outs), N3 (Schublade). — ✅ N1a+N1b umgesetzt (Branch `perf/idle-baseline`); **nächster Schritt: Nachmessung (User)**.
3. **Track 3 — Konsistenz (restliches Fehlerpotenzial; bewusst VOR dem Struktur-Track, weil die Fixes Grep-basiert über den heutigen Dateistand laufen):** F5 → F6 (Radar an `world_half` koppeln, halbe Weltgröße bleibt) → F7 → F9 (= W9; Helper wandern später beim Split als Einheit mit) → F8 (distanzabhängiges Feuer-Gate als eigener PR + Doku-Punkte). — ✅ komplett umgesetzt (Branch `consistency/track3`).
4. **Track 4 — Struktur (Wartbarkeit, zuletzt):** W1 (constants.py, zusammen mit W8-Tabelle) → W2 (models/util) → W4 (Mixin-Split, 9 Einzel-Commits) → W5 (bzbot.py dünn) → W7 → W3 (_on_set_var-Tabelle, mit Snapshot-Test). Zu diesem Zeitpunkt sind alle P/F-Punkte abgeschlossen → keine Referenz-Entwertung; sollte doch ein Punkt offen sein, gilt Regel 4 aus „Wechselwirkungen" (Referenzen im Plan pro W-PR nachziehen, Landkarten-Tabelle nutzen).

## Verifikation (gesamt)

- **Regressionsschutz:** `python -m pytest` (886 Tests) nach jedem Einzelschritt; `pytest -m perf -s` vor/nach Performance-Änderungen.
- **Äquivalenztests als Abnahmekriterium** für P1/P6 (exakte Gleichheit alt vs. neu, Muster `TestLosRayGridEquivalence` / „0 Mismatch über alle HIX-Knoten").
- **Server-Profiling** vor Beginn und nach Track 1+2: `py-spy record --pid <pid> --duration 120 --rate 250` im Container (SYS_PTRACE nötig) + `docker stats` über 10 Minuten; Ziel-Metrik: CPU-% pro Bot-Prozess.
- **Verhaltens-Smoke-Test:** 4-Bot-Lokallauf via `bot_manager.py` (wie in bzbot-pyspy.ps1) über 10 Minuten, Log auf WARNING/ERROR prüfen; auf dem Server 1 Bot mit `--log-level INFO` beobachten (Kills, Dodges, keine [nr]-Meldungen).
- **Langzeit-Check für F3:** Bot 2h auf Rico-Server laufen lassen, `len(self._ricochet_paths)` periodisch loggen (Debug-Option) → muss beschränkt bleiben.
