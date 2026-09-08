// A small, local Lucide sprite; preserve existing IDs and the original brand mark.
// Source: https://lucide.dev/guide/static (ISC, included in workbench-licenses.txt).
import { readFile, writeFile } from "node:fs/promises";

export const icons = {
  more: "ellipsis", refresh: "refresh-cw", sun: "sun", moon: "moon", monitor: "monitor",
  menu: "menu", logo: "search", user: "user-round", plus: "plus", target: "radar",
  download: "download", filter: "sliders-horizontal", bell: "bell", list: "list-ordered",
  film: "clapperboard", msg: "message-circle", bolt: "zap", check: "check", x: "x",
  info: "info", shield: "shield-check", inbox: "inbox", heart: "heart", image: "image",
  play: "play", send: "send", prev: "chevron-left", next: "chevron-right",
  first: "chevrons-left", last: "chevrons-right", clock: "clock-3", hash: "hash",
  eye: "eye", "eye-off": "eye-off", card: "contact-round", "heart-up": "thumbs-up",
  folder: "folder-open", copy: "copy", external: "external-link", trash: "trash-2",
  settings: "settings-2", overview: "panels-top-left", captions: "captions",
  automation: "bot-message-square", library: "library", login: "log-in", qr: "qr-code",
  cookie: "key-round", fingerprint: "fingerprint", network: "network", globe: "globe",
  alert: "circle-alert", offline: "wifi-off", chevron: "chevron-down", edit: "square-pen",
  save: "save", sparkles: "sparkles", palette: "palette", scan: "scan-line", calendar: "calendar-days", link: "link",
};

export async function buildIcons() {
  const html = await readFile("app/web/index.html", "utf8");
  const sprite = html.match(/<!-- icon sprite -->[\s\S]*?<\/svg>/)?.[0];
  if (!sprite) throw new Error("Missing inline icon sprite");
  // Brand geometry is canonical, not whatever a previous icon build left behind.
  const brandSource = await readFile("frontend/brand.svg", "utf8");
  const brandSvg = brandSource.match(/<svg\s([^>]+)>([\s\S]*?)<\/svg>/);
  if (!brandSvg) throw new Error("Missing original CreatorHub brand mark");
  const brand = `<symbol id="i-brand" ${brandSvg[1].replace(/\s*xmlns="[^"]+"/, "")}>${brandSvg[2].trim()}</symbol>`;
  const symbols = await Promise.all(Object.entries(icons).map(async ([id, name]) => {
    const source = await readFile(`node_modules/lucide-static/icons/${name}.svg`, "utf8");
    const body = source.match(/<svg[\s\S]*?>([\s\S]*?)<\/svg>/)?.[1].trim().replace(/\s+/g, " ");
    if (!body) throw new Error(`Invalid Lucide icon: ${name}`);
    return `<symbol id="i-${id}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${body}</symbol>`;
  }));
  const output = `<!-- icon sprite -->\n<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false" data-icon-set="lucide">\n  ${[brand, ...symbols].join("\n  ")}\n</svg>`;
  await writeFile("app/web/index.html", html.replace(sprite, output));
}
