ls
cd projects
ls -la
cd log-parser
git status
git log --oneline | head
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
vim parser.py
pytest -q tests/test_parser.py
git diff
git add parser.py tests/test_parser.py
git commit -m "Handle empty lines in the parser"
deactivate
cd ..
ls
cd notes
vim todo.md
cat meeting-2026-09-12.md
cd ~
df -h
free -m
htop
cd projects/scripts
chmod +x backup.sh
./backup.sh --dry-run
vim backup.sh
./backup.sh --dry-run
cd ../log-parser
source .venv/bin/activate
python3 parser.py samples/app.log | head
python3 parser.py samples/app.log --level error
pytest -q
git stash
git pull
git stash pop
pytest -q
git push
deactivate
history | grep pytest
cd ~/projects/notes
vim reading.md
ls -lh ~/projects
du -sh ~/projects/*
python3
cd ~
