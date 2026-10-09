import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/** Test provenance: hash inputs, not generated output, so committing logs cannot change their identity. */
export function evidence(root: string, files: readonly string[], command: string) {
  const git = (...args: string[]) => execFileSync("git", ["-C", root, ...args], { encoding: "utf8" }).trim();
  return {
    testedCommit: git("rev-parse", "HEAD"),
    inputTree: git("rev-parse", "HEAD^{tree}"),
    dirtyInputs: git("status", "--porcelain=v1", "--", ...files),
    inputSha256: Object.fromEntries(files.map(file => [file, createHash("sha256").update(readFileSync(join(root, file))).digest("hex")])),
    command,
    installCommand: process.env.COPPERTOP_INSTALL_COMMAND ?? "not recorded; do not infer installation flags",
    node: process.version,
    platform: process.platform,
    architecture: process.arch,
  };
}
