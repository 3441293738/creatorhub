import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
import { buildIcons } from "./icons.mjs";

await buildIcons();

const result = await build({
  entryPoints: ["frontend/workbench.jsx"], outfile: "app/web/workbench.js",
  bundle: true, minify: true, format: "iife", target: ["chrome110", "safari16"],
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
