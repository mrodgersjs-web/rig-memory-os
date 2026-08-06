import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.rodgersintelligence.gothicreckoning',
  appName: 'Gothic Reckoning',
  webDir: 'public',
  bundledWebRuntime: false,
  server: {
    // Navigation stays in-app. The web client falls back to a deterministic
    // tutorial table if the production OpenViking API is unavailable.
    androidScheme: 'https',
  },
  ios: { contentInset: 'automatic' },
};

export default config;
