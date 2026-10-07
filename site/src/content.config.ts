import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import type { Loader } from 'astro/loaders';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';
import { ADR_DIR, loadPickedAdrs } from './lib/adr';

/**
 * Reads the curated ADRs straight from ../docs/adr at build time (never copied).
 * ADR files have no frontmatter, so the title/status/date come from their header lines.
 */
const adrLoader: Loader = {
  name: 'jarvis-adr-loader',
  async load({ store, parseData, renderMarkdown, generateDigest, watcher }) {
    watcher?.add(ADR_DIR);
    store.clear();
    for (const adr of loadPickedAdrs()) {
      const { body, ...fields } = adr;
      const data = await parseData({ id: adr.number, data: fields });
      store.set({
        id: adr.number,
        data,
        body,
        digest: generateDigest(`${adr.title}${adr.status}${adr.date}${body}`),
        rendered: await renderMarkdown(body),
      });
    }
  },
};

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
  adr: defineCollection({
    loader: adrLoader,
    schema: z.object({
      number: z.string(),
      title: z.string(),
      shortTitle: z.string(),
      status: z.string(),
      date: z.string(),
    }),
  }),
};
