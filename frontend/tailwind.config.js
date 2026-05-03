/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'terminal-bg': '#05070A',
        'terminal-bg-light': '#0B0F14',
        'neon-cyan': '#47D7FF',
        'neon-green': '#39FF14',
        'neon-red': '#FF4D4D',
        'text-secondary': '#7F8A96',
        'text-primary': '#E6EEF7',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'IBM Plex Mono', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        'neon-cyan': '0 0 10px rgba(71, 215, 255, 0.5), 0 0 20px rgba(71, 215, 255, 0.3)',
        'neon-green': '0 0 10px rgba(57, 255, 20, 0.5)',
        'neon-red': '0 0 10px rgba(255, 77, 77, 0.5)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
      },
      keyframes: {
        glow: {
          '0%': { opacity: '0.8' },
          '100%': { opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}
