module.exports = {
  apps: [
    {
      name: 'llm-web',
      script: '/mnt/c/Users/xtech/Projects/llm_web/start.sh',
      interpreter: '/bin/bash',
      cwd: '/mnt/c/Users/xtech/Projects/llm_web',
      autorestart: true,
      watch: false,
      max_memory_restart: '512M',
      env: {
        PATH: '/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:/home/xtech/.local/bin:/mnt/c/Users/xtech/AppData/Roaming/npm:/mnt/c/Program Files/nodejs',
        HOME: '/home/xtech',
        USER: 'xtech',
        DOCKER_HOST: 'unix:///var/run/docker.sock',
        CLAUDE_CONFIG_DIR: '/home/xtech/.claude',
        SEND_ENTER_DELAY: '0.3',
        ASR_API_URL: 'https://whisper-asr.2dox.uz/qwen/transcribe',
        ASR_TOKEN: '6513f2c159f80802def3b8f6594a60b4d3d3c92ff3e01686b2afaa1e17bfe24f',
        ASR_LANGUAGE: 'ru',
      }
    },
    {
      name: 'llm-web-watchdog',
      script: '/mnt/c/Users/xtech/Projects/llm_web/safe_deploy.py',
      args: 'watchdog',
      interpreter: '/usr/bin/python3',
      cwd: '/mnt/c/Users/xtech/Projects/llm_web',
      autorestart: true,
      watch: false,
      env: {
        PATH: '/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:/home/xtech/.local/bin',
        HOME: '/home/xtech',
      }
    }
  ]
}
