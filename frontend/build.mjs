import { build, transform } from "esbuild";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { buildIcons } from "./icons.mjs";

const BROWSER_TARGETS = ["chrome110", "safari16"];
const GENERATED_HTML_COMMENT = "<!-- AUTO-GENERATED FROM frontend/html/*.html & frontend/styles/base.css BY npm run build:ui — DO NOT EDIT DIRECTLY -->\n";
const GENERATED_CSS_BANNER = "/* AUTO-GENERATED FROM frontend/agent.css BY npm run build:ui — DO NOT EDIT DIRECTLY */\n";
const GENERATED_JS_BANNER = "/* AUTO-GENERATED FROM frontend/app/*.js BY npm run build:ui — EDIT SOURCE MODULES IN frontend/app/ */\n";

// 1. Assemble and validate frontend/styles/base.css + frontend/html/*.html -> app/web/index.html
const baseCss = await readFile("frontend/styles/base.css", "utf8");
await transform(baseCss, { loader: "css", target: BROWSER_TARGETS, sourcefile: "frontend/styles/base.css" });
const htmlPartNames = (await readdir("frontend/html"))
  .filter(name => name.endsWith(".html"))
  .sort();
const htmlParts = [];
for (const name of htmlPartNames) {
  htmlParts.push(await readFile(`frontend/html/${name}`, "utf8"));
}
const joinedHtml = htmlParts.join("");
const cssPlaceholder = /\/\* __BASE_CSS__ \*\/\r?\n/;
if (!cssPlaceholder.test(joinedHtml)) {
  throw new Error("Missing /* __BASE_CSS__ */ placeholder in frontend/html/01-shell-header.html");
}
const rawHtml = joinedHtml.replace(cssPlaceholder, () => baseCss);
const assembledHtml = rawHtml.replace(/^<!DOCTYPE html>\r?\n/i, match => match + GENERATED_HTML_COMMENT);
await writeFile("app/web/index.html", assembledHtml);

// 2. Refresh inline Lucide + brand SVG sprite inside assembled app/web/index.html
await buildIcons();

// 3. Assemble and syntax/target-validate modular frontend/app/*.js -> app/web/app.js
const appPartNames = (await readdir("frontend/app"))
  .filter(name => name.endsWith(".js"))
  .sort();
const appChunks = [];
for (const name of appPartNames) {
  const partPath = `frontend/app/${name}`;
  const partSource = await readFile(partPath, "utf8");
  await transform(partSource, { loader: "js", target: BROWSER_TARGETS, sourcefile: partPath });
  appChunks.push(partSource.trimEnd());
}
const assembledAppJs = GENERATED_JS_BANNER + appChunks.join("\n\n") + "\n";
await transform(assembledAppJs, { loader: "js", target: BROWSER_TARGETS, sourcefile: "app/web/app.js" });
await writeFile("app/web/app.js", assembledAppJs);

// 4. Sync agent stylesheet with explicit generated banner
await writeFile("app/web/agent.css", GENERATED_CSS_BANNER + await readFile("frontend/agent.css", "utf8"));

// 5. Bundle React workbench & AI assistant islands
const result = await build({
  entryPoints: ["frontend/workbench.jsx"], outfile: "app/web/workbench.js",
  bundle: true, minify: true, format: "iife", target: BROWSER_TARGETS,
  define: { "process.env.NODE_ENV": '"production"' }, legalComments: "linked", metafile: true,
});
const packages = [...new Set(Object.keys(result.metafile.inputs).map(path => path.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/)?.[1]).filter(Boolean))].sort();
packages.push("lucide-static");
const licenses = [];
for (const name of packages) {
  const metadata = JSON.parse(await readFile(`node_modules/${name}/package.json`, "utf8"));
  let license = "";
  for (const filename of ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.MD"]) {
    try { license = await readFile(`node_modules/${name}/${filename}`, "utf8"); break; } catch {}
  }
  // This upstream npm tarball omits its license; retain the upstream copy locally.
  if (!license && name === "react-remove-scroll-bar") license = await readFile("frontend/licenses/react-remove-scroll-bar.txt", "utf8");
  if (!license) throw new Error(`Missing license for bundled package ${name}`);
  licenses.push(`${name}@${metadata.version}\n${"=".repeat(60)}\n${license}`);
}
await writeFile("app/web/workbench-licenses.txt", licenses.join("\n\n"));
// The bundle is committed: serving the UI needs no build toolchain at startup.
