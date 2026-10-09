import { build } from 'esbuild';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
const root = fileURLToPath(new URL('.', import.meta.url));
const result = await build({
  entryPoints: [resolve(root, 'src/app.js')], bundle: true, write: false,
  format: 'iife', platform: 'browser', target: ['es2022'], minify: true,
});
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const css = await readFile(resolve(root, 'src/style.css'), 'utf8');
const html = (await readFile(resolve(root, 'src/index.html'), 'utf8'))
  .replace('/* STYLES */', css).replace('/* SCRIPT */', js);
const dest = resolve(root, '../mcp_server/chatgpt_ui/control-center.html');
await mkdir(resolve(root, '../mcp_server/chatgpt_ui'), { recursive: true });
await writeFile(dest, html);
console.log(`Built ${dest} (${Buffer.byteLength(html)} bytes)`);
