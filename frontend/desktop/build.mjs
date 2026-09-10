import { build } from "esbuild";
import { readFile, writeFile, mkdir } from "node:fs/promises";
const out = "desktop/web";
await mkdir(out, { recursive: true });
const result = await build({ entryPoints: ["frontend/desktop/app.jsx"], outfile: `${out}/app.js`, bundle: true, minify: true, format: "iife", target: ["chrome110"], define: { "process.env.NODE_ENV": '"production"' }, legalComments: "linked", metafile: true });
await writeFile(`${out}/index.html`, await readFile("frontend/desktop/index.html", "utf8"));
const icons = ["file-text", "layout-dashboard", "activity", "book-open", "sliders-horizontal", "monitor", "shield-check", "search", "sun", "moon", "check", "x", "refresh-cw", "wifi-off", "unplug", "arrow-up-right", "loader-circle", "play", "stethoscope", "file-video", "image", "send", "copy", "ellipsis", "panel-bottom-close", "square", "folder", "folder-open", "arrow-right", "arrow-left", "circle-alert", "triangle-alert", "circle-dot", "chevron-right", "search-x", "check-check", "info", "power", "download", "corner-down-left"];
const symbols = await Promise.all(icons.map(async name => { const svg = await readFile(`node_modules/lucide-static/icons/${name}.svg`, "utf8"); return `<symbol id="${name}" viewBox="0 0 24 24">${svg.match(/<svg[^>]*>([\s\S]*?)<\/svg>/)[1]}</symbol>`; }));
const brand = await readFile("frontend/brand.svg", "utf8");
symbols.push(`<symbol id="brand" viewBox="0 0 24 24">${brand.match(/<svg[^>]*>([\s\S]*?)<\/svg>/)[1]}</symbol>`);
await writeFile(`${out}/icons.svg`, `<svg xmlns="http://www.w3.org/2000/svg"><defs>${symbols.join("")}</defs></svg>`);
// Preserve upstream notices for every bundled UI dependency.
const packages = [...new Set(Object.keys(result.metafile.inputs).map(p => p.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/)?.[1]).filter(Boolean)), "lucide-static"];
const notices = [];
for (const name of packages) {
  const metadata = JSON.parse(await readFile(`node_modules/${name}/package.json`, "utf8"));
  let license;
  for (const file of ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.MD"]) { try { license = await readFile(`node_modules/${name}/${file}`, "utf8"); break; } catch {} }
  if (!license && name === "react-remove-scroll-bar") license = await readFile("frontend/licenses/react-remove-scroll-bar.txt", "utf8");
  if (!license) throw Error(`Missing license: ${name}`);
  notices.push(`${name}@${metadata.version}\n${license}`);
}
await writeFile(`${out}/THIRD-PARTY-NOTICES.txt`, notices.join("\n\n"));
console.log("Desktop renderer built: desktop/web");
