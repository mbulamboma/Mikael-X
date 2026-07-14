@echo off
REM Cerveau data Mikael-X — boucle 1 run/heure, AUTO-RESTART si crash.
REM A lancer au demarrage du VPS (Planificateur de taches, voir README).
cd /d %~dp0
:loop
python macro_service.py --loop 60
echo [%date% %time%] service arrete (code %errorlevel%) — relance dans 60 s >> service_restarts.log
timeout /t 60 /nobreak >nul
goto loop
