@echo off
REM Lance le cerveau data en boucle (1 run/heure).
REM A garder ouvert sur le VPS pendant tout le forward-test.
cd /d %~dp0
python macro_service.py --loop 60
pause
