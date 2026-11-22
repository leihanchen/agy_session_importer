import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");
const outPath = path.join(repoRoot, "skills", "resume-session", "ui", "app.html");

function escapeInlineScript(jsCode) {
  return jsCode.replace(/<\/script/gi, "<\\/script");
}

function escapeInlineStyle(cssCode) {
  return cssCode.replace(/<\/style/gi, "<\\/style");
}

async function generateHtml() {
  const cssPath = path.join(__dirname, "styles.css");
  const cssRaw = fs.readFileSync(cssPath, "utf8").trim();

  const result = await build({
    entryPoints: [path.join(__dirname, "app.ts")],
    bundle: true,
    write: false,
    format: "iife",
    platform: "browser",
    target: ["es2020"],
    minify: true,
    legalComments: "none",
  });

  if (!result.outputFiles || result.outputFiles.length === 0) {
    throw new Error("esbuild produced no output files");
  }

  const jsCode = escapeInlineScript(result.outputFiles[0].text.trim());
  const cssCode = escapeInlineStyle(cssRaw);

  return [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "<head>",
    '  <meta charset="utf-8">',
    '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
    "  <title>Session Importer</title>",
    "  <style>",
    cssCode,
    "  </style>",
    "</head>",
    "<body>",
    '  <div id="app"></div>',
    "  <script>",
    jsCode,
    "  </script>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

async function main() {
  const checkOnly = process.argv.includes("--check");
  const expectedHtml = await generateHtml();

  if (checkOnly) {
    if (!fs.existsSync(outPath)) {
      console.error(`Missing built UI bundle: ${outPath}`);
      process.exit(1);
    }
    const existingHtml = fs.readFileSync(outPath, "utf8");
    if (existingHtml !== expectedHtml) {
      console.error(
        `UI bundle out of date: ${outPath} does not match fresh build. Run: node ui_src/build.mjs`
      );
      process.exit(1);
    }
    console.log(`UI bundle is up to date (${Buffer.byteLength(existingHtml, "utf8")} bytes).`);
    return;
  }

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, expectedHtml, "utf8");
  console.log(`Built ${outPath} (${Buffer.byteLength(expectedHtml, "utf8")} bytes).`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
