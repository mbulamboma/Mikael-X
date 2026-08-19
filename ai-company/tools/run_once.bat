@echo off
REM Un run UNIQUE du cerveau data — concu pour le Planificateur de taches
REM (mode recommande VPS : l'OS relance chaque heure, aucun processus
REM permanent a surveiller ; un run rate n'empeche pas le suivant).
cd /d %~dp0
python macro_service.py >> service_runs.log 2>&1
