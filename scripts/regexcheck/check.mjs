// The JavaScript half of scripts/check_regexes.py: finds the regular
// expressions in TypeScript, Svelte and JavaScript (with the TypeScript
// parser, so a "/" in a string or a comment is never taken for one), and runs
// recheck on every pattern, those Python found included. Reads
// {root, dirs, patterns} on stdin; writes {checked, problems}; exits 1 on a problem.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { check } from "recheck";

const input = JSON.parse(readFileSync(0, "utf8"));
const root = input.root;
import ts from "typescript"; // pinned in this folder's package.json (the app's version)

const SKIP = new Set(["node_modules", "target", ".git", "dist", "build", ".svelte-kit"]);
const EXTS = [".ts", ".js", ".mjs", ".svelte"];

function* walk(dir) {
  let entries = [];
  try { entries = readdirSync(dir); } catch { return; }
  for (const name of entries.sort()) {
    if (SKIP.has(name)) continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) yield* walk(p);
    else if (EXTS.some((e) => name.endsWith(e))) yield p;
  }
}

// Svelte: the code is in <script> blocks; the markup's {expressions} are short and checked by svelte-check.
function scripts(path, text) {
  if (!path.endsWith(".svelte")) return [[text, 0]];
  const out = [];
  let at = 0;
  for (;;) {
    const open = text.indexOf("<script", at);
    if (open < 0) break;
    const start = text.indexOf(">", open) + 1;
    const end = text.indexOf("</script>", start);
    if (start <= 0 || end < 0) break;
    out.push([text.slice(start, end), text.slice(0, start).split("\n").length - 1]);
    at = end;
  }
  return out;
}

const patterns = [...input.patterns];
for (const dir of input.dirs) {
  for (const path of walk(join(root, dir))) {
    const rel = relative(root, path);
    for (const [code, lineOffset] of scripts(path, readFileSync(path, "utf8"))) {
      const sf = ts.createSourceFile(rel, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
      const where = (node) => `${rel}:${sf.getLineAndCharacterOfPosition(node.getStart()).line + 1 + lineOffset}`;
      const visit = (node) => {
        if (node.kind === ts.SyntaxKind.RegularExpressionLiteral) {
          const text = node.text;
          const cut = text.lastIndexOf("/");
          patterns.push({ where: where(node), source: text.slice(1, cut), flags: text.slice(cut + 1), lang: "js" });
        } else if (ts.isNewExpression(node) || ts.isCallExpression(node)) {
          if (node.expression.getText(sf) === "RegExp" && node.arguments?.length) {
            const [pat, flags] = node.arguments;
            if (ts.isStringLiteralLike(pat) && (!flags || ts.isStringLiteralLike(flags))) {
              patterns.push({ where: where(node), source: pat.text, flags: flags ? flags.text : "", lang: "js" });
            } else {
              patterns.push({ where: where(node), unresolved: true, lang: "js",
                              runtime: /regex:\s*runtime/.test(code.slice(Math.max(0, node.getFullStart() - 200), node.getEnd())) });
            }
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(sf);
    }
  }
}

const checked = [];
const problems = [];
for (const p of patterns) {
  if (p.unresolved) {
    if (!p.runtime) problems.push(`${p.where}: a RegExp built from a value only known at run time: build it from literals, or mark it with \`// regex: runtime (why it is safe)\``);
    continue;
  }
  try {
    new RegExp(p.source, p.flags.replace(/[^dgimsuyv]/g, ""));
  } catch (e) {
    problems.push(`${p.where}: doesn't compile${p.lang === "python" ? " as JavaScript (after conversion from Python)" : ""}: ${e.message}`);
    continue;
  }
  const r = await check(p.source, p.flags.replace(/[gyd]/g, ""), { timeout: 10000 });
  let verdict = r.status === "safe" ? "linear" : r.status;
  if (r.status === "vulnerable") {
    const kind = r.complexity.type === "exponential" ? "exponential" : `polynomial (degree ${r.complexity.degree})`;
    verdict = r.complexity.type === "exponential" ? "exponential" : `poly-${r.complexity.degree}`;
    problems.push(`${p.where}: ${kind} backtracking: ${JSON.stringify(p.python ?? p.source)}; slow on ${r.attack.pattern}`);
  } else if (r.status !== "safe") {
    problems.push(`${p.where}: couldn't be proven safe (${r.error?.kind ?? r.status}): ${JSON.stringify(p.python ?? p.source)}; simplify it`);
  }
  checked.push({ where: p.where, source: p.source, python: p.python, lang: p.lang, verdict });
}
process.stdout.write(JSON.stringify({ checked, problems }));
process.exit(problems.length ? 1 : 0);
