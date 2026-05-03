#!/bin/bash
# Start the arb bot with dashboard
cd /home/admin/polymarket_money_maker/polymarket_arb2
source venv/bin/activate

# Kill any existing process
pkill -f "python3 -m app.main" 2>/dev/null

echo "Starting Polymarket Arb Bot with Dashboard..."
nohup python3 -m app.main > /tmp/arb_bot.log 2>&1 &
PID=$!
echo "Bot started with PID: $PID"
echo "Dashboard API: http://0.0.0.0:8080"
echo "Frontend: file://$PWD/frontend/index.html"
echo "Token: YOUR_DASHBOARD_TOKEN"
echo ""
echo "To view logs: tail -f /tmp/arb_bot.log"
echo "To stop: kill $PID"
