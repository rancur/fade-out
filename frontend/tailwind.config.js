/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: '#7CB342',
        secondary: '#FF8A65',
        accent: '#4FC3F7',
        gold: '#FFD54F',
        dark: '#1B2631',
        surface: '#0D1117',
        'surface-light': '#161B22',
        cyber: {
          cyan: '#00E5FF',
          magenta: '#E040FB',
          lime: '#76FF03',
          orange: '#FFAB40',
          red: '#FF1744',
        },
      },
      fontFamily: {
        pixel: ['"Press Start 2P"', 'monospace'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      boxShadow: {
        neon: '0 0 5px theme(colors.primary), 0 0 20px theme(colors.primary / 30%)',
        'neon-cyan': '0 0 5px theme(colors.cyber.cyan), 0 0 20px theme(colors.cyber.cyan / 30%)',
        'neon-magenta': '0 0 5px theme(colors.cyber.magenta), 0 0 20px theme(colors.cyber.magenta / 30%)',
        'neon-red': '0 0 5px theme(colors.cyber.red), 0 0 20px theme(colors.cyber.red / 30%)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
        'slide-in': 'slideIn 0.3s ease-out',
      },
      keyframes: {
        glow: {
          '0%': { boxShadow: '0 0 5px currentColor, 0 0 10px currentColor' },
          '100%': { boxShadow: '0 0 10px currentColor, 0 0 30px currentColor, 0 0 50px currentColor' },
        },
        slideIn: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}
