# 24.03.26-tg-bot-bingx
телеграм бот для отслеживания фандинга определённых монет с биржи bing x

cat > run.sh << 'EOF'
#!/bin/bash
cd /opt/python/bingx_tg_funding
source venv/bin/activate
python main_tg.py
EOF