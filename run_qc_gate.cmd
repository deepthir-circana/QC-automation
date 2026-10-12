@echo off
REM Double-click to start QC Gate. First run installs packages.
cd /d "%~dp0"
python -c "import streamlit, plotly, pyodbc" 2>nul || python -m pip install -r requirements.txt
python -m streamlit run app.py
pause
