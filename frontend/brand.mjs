// Keep static HTML, installer labels and the text mark in sync with app/brand.json.
import { readFile, writeFile } from "node:fs/promises";
export async function syncBrand() {
  const brand = JSON.parse(await readFile("app/brand.json", "utf8"));
  for (const path of ["app/web/index.html", "frontend/desktop/index.html"]) {
    let html = await readFile(path, "utf8");
    html = html.replace(/<title>[^<]*/, `<title>${brand.name}${path.includes("desktop") ? " · 启动中心" : ""}`);
    if (!path.includes("desktop")) html = html.replace(/<h1>[^<]*/, `<h1>${brand.name}`);
    await writeFile(path, html);
  }
  await writeFile("frontend/brand.svg", `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect x="2" y="2" width="252" height="252" rx="54" fill="white"/><text x="128" y="186" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="190" fill="${brand.accent}">${brand.mark}</text></svg>\n`);
}
