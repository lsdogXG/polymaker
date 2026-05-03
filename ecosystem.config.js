module.exports = {
  apps: [
    {
      name: 'polymarket-backend',
      script: '.venv/bin/python',
      args: '-m app.main',
      cwd: '/home/admin/polymarket_arb2',
      interpreter: 'none',
      env: {
        // 从 .env 文件加载，这里可以覆盖
        DASHBOARD_PORT: '8080',
      },
      // 自动重启
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 5000,
      // 日志
      error_file: './logs/backend-error.log',
      out_file: './logs/backend-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      merge_logs: true,
    },
    {
      name: 'polymarket-frontend',
      script: '.venv/bin/python',
      args: 'serve_frontend.py',
      cwd: '/home/admin/polymarket_arb2',
      interpreter: 'none',
      env: {
        FRONTEND_PORT: '3001',
      },
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 3000,
      error_file: './logs/frontend-error.log',
      out_file: './logs/frontend-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      merge_logs: true,
    },
  ],
};
