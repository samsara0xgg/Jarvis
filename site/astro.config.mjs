// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { loadPickedAdrs } from './src/lib/adr.ts';

const adrs = loadPickedAdrs();

// Chinese sidebar labels come from `translations` (BCP-47 key "zh-CN").
const t = (/** @type {string} */ zh) => ({ 'zh-CN': zh });

// https://astro.build/config
export default defineConfig({
  site: 'https://samsara0xgg.github.io',
  base: '/Jarvis',
  integrations: [
    starlight({
      title: 'Jarvis',
      description: 'A voice assistant that lives by your MacBook notch.',
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/samsara0xgg/Jarvis' }],
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        'zh-cn': { label: '简体中文', lang: 'zh-CN' },
      },
      customCss: ['./src/styles/custom.css'],
      sidebar: [
        { link: '/', label: 'Home', translations: t('首页') },
        {
          label: 'Feature tour',
          translations: t('功能导览'),
          items: [
            { slug: 'features', label: 'All features', translations: t('全部功能') },
            'features/meet-her',
            'features/talking',
            'features/today',
            'features/agents',
            'features/memory',
            'features/mail',
            'features/proactive',
            'features/usage',
            'features/plugins',
            'features/try-the-demo',
          ],
        },
        {
          label: 'Design & architecture',
          translations: t('设计与架构'),
          items: [
            { slug: 'architecture', label: 'Overview', translations: t('概览') },
            'architecture/design-notes',
            {
              // ADR pages are English only; /zh-cn/ serves the same English content under the Chinese UI.
              label: 'Decisions',
              translations: t('决策记录'),
              items: [
                { label: 'All decisions', translations: t('全部决策'), link: '/architecture/decisions/' },
                ...adrs.map((a) => ({
                  label: `${a.number} · ${a.shortTitle}`,
                  link: `/architecture/decisions/${a.number}/`,
                })),
              ],
            },
          ],
        },
      ],
    }),
  ],
});
