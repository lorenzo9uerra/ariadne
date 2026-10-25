cd /workspace
ls -la
python3 solve.py | tee run.log
grep -o 'flag{[^}]*}' run.log
echo '{{value}}' > answer.txt
