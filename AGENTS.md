# BZFlag Bot

## Zielstellung

Ziel ist es einen Bot für BZFlag zu implementieren. BZFlag ist ein 3D Arcade-Spiel mit Panzern (Tanks) mit Superkräften. Diese Panzer können durch Power-Ups (Flaggen/Flags) oder bestimmte Optionen
zusätzliche Fähigkeiten erlangen, wie bspw. Springen.

Der Bot wird genauso, wie der Dedicated Server (bzfs v2.4) auf dem der Bot hauptsächlich spielen wird, in einem Docker-Container betrieben. Da die `-disableBots` Option im Server gesetzt sein wird,
wodurch sowohl der Autopilot als auch die mitgebrachten Bots des BZFlag-Client nicht erlaubt sind, muss der Bot sich wie ein normaler Client verbinden und verhalten.

---

## Wichtige Dokumente

- BZFLAG.md: Grundlegende Spielphysik/-regeln, Tabelle der Flaggen und ähnliches
- FSD.md: Functional Specification Document - Beschreibt die gewünschte/geplante Funktionalität des Bots.
- DEVELOPER.md: Weiterführender Dokumentation zu komplexeren Codestellen oder Architektur-/Designentscheidungen.
