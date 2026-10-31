@echo off
rem Opens the pi coding agent (pi.dev) on the model the Local Agent app is serving.
rem Start the model in the app first (Model server page). The port in pi\models.json must match the app's.
rem pi keeps its settings and sessions for this in the pi folder next to this file.
set "PI_CODING_AGENT_DIR=%~dp0pi"
where pi >nul 2>nul || (echo pi isn't installed. Install it with: npm install -g @earendil-works/pi-coding-agent & pause & exit /b 1)
pi --model local-agent/local %*
