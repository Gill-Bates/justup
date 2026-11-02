//
// tests/js/workflow-contracts.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import yaml from "js-yaml";

const workflow = yaml.load(readFileSync(new URL("../../.github/workflows/docker-build.yml", import.meta.url), "utf8"));

function stepIndex(steps, predicate, label) {
  const index = steps.findIndex(predicate);
  assert.notEqual(index, -1, `${label} step is missing`);
  return index;
}

function assertReleaseGates(document) {
  const buildSteps = document.jobs.build.steps;
  const purityIndex = stepIndex(
    buildSteps,
    (step) => step.name?.startsWith("Verify runtime image purity") && step.run?.includes("tools/verify-runtime-image.py layers"),
    "runtime image purity",
  );
  const purity = buildSteps[purityIndex];
  assert.equal(purity["continue-on-error"], undefined, "purity failure must fail the build");
  assert.equal(purity.if, undefined, "purity verification must not be conditional");
  assert.match(purity.run, /^\s*set -euo pipefail/m);
  assert.match(
    purity.run,
    /^\s*python3 tools\/verify-runtime-image\.py layers "\$archive"\s*$/m,
    "purity verifier must run without masking its exit code",
  );
  const pushIndex = stepIndex(buildSteps, (step) => step.name === "Log in to Docker Hub (push)", "registry push login");
  assert.ok(purityIndex < pushIndex, "runtime image purity must pass before registry push credentials are used");

  const manifest = document.jobs["create-manifest"];
  assert.deepEqual([...manifest.needs].sort(), ["build", "meta"], "manifest job must depend on metadata and all builds");
  assert.match(manifest.if, /needs\.build\.result\s*==\s*'success'/);
  const steps = manifest.steps;
  const candidateIndex = stepIndex(steps, (step) => step.name === "Create candidate index", "candidate index creation");
  const verifyIndex = stepIndex(
    steps,
    (step) => step.name === "Verify candidate index" && step.run?.includes("tools/verify-manifest.py verify manifest-candidate.json digests"),
    "candidate manifest verification",
  );
  const publishIndex = stepIndex(steps, (step) => step.name === "Publish version tag", "version publication");
  const verify = steps[verifyIndex];
  assert.equal(verify["continue-on-error"], undefined, "candidate verification failure must fail the manifest job");
  assert.match(verify.run, /^\s*set -euo pipefail/m);
  assert.ok(candidateIndex < verifyIndex && verifyIndex < publishIndex);
  assert.match(steps[publishIndex].run, /imagetools create --tag "\$\{IMAGE_NAME\}:\$\{VERSION\}" "\$\{IMAGE_NAME\}@\$\{INDEX_DIGEST\}"/);
  assert.match(steps[publishIndex].run, /verify manifest-version\.json digests/);
}

test("release publication is gated by image purity and verified candidate manifests", () => {
  assert.doesNotThrow(() => assertReleaseGates(workflow));

  const withoutPurityGate = JSON.parse(JSON.stringify(workflow));
  withoutPurityGate.jobs.build.steps = withoutPurityGate.jobs.build.steps.filter(
    (step) => !step.name?.startsWith("Verify runtime image purity"),
  );
  assert.throws(() => assertReleaseGates(withoutPurityGate), /purity step is missing/);

  const maskedPurityFailure = JSON.parse(JSON.stringify(workflow));
  const purityStep = maskedPurityFailure.jobs.build.steps.find((step) => step.name?.startsWith("Verify runtime image purity"));
  purityStep.run = purityStep.run.replace(
    'python3 tools/verify-runtime-image.py layers "$archive"',
    'python3 tools/verify-runtime-image.py layers "$archive" || true',
  );
  assert.throws(() => assertReleaseGates(maskedPurityFailure), /purity verifier must run/);

  const manifestWithoutBuildDependency = JSON.parse(JSON.stringify(workflow));
  manifestWithoutBuildDependency.jobs["create-manifest"].needs = ["meta"];
  assert.throws(() => assertReleaseGates(manifestWithoutBuildDependency), /manifest job must depend/);
});

test("runtime image purity step fails closed when the layer verifier rejects the archive", () => {
  const step = workflow.jobs.build.steps.find((item) => item.name?.startsWith("Verify runtime image purity"));
  const temp = mkdtempSync(path.join(tmpdir(), "justup-review-fixes-"));
  const bin = path.join(temp, "bin");
  const calls = path.join(temp, "python-calls.txt");
  try {
    mkdirSync(bin);
    const docker = path.join(bin, "docker");
    const python = path.join(bin, "python3");
    writeFileSync(docker, [
      "#!/bin/sh",
      'if [ "$1" = "save" ]; then',
      '  [ "$3" = "-o" ] || exit 90',
      '  : > "$4"',
      "fi",
      "exit 0",
      "",
    ].join("\n"));
    writeFileSync(python, [
      "#!/bin/sh",
      'printf "%s\\n" "$*" >> "$PYTHON_CALLS"',
      'if [ "$1" = "tools/verify-runtime-image.py" ] && [ "$2" = "layers" ]; then exit 42; fi',
      "exit 0",
      "",
    ].join("\n"));
    chmodSync(docker, 0o755);
    chmodSync(python, 0o755);

    const result = spawnSync("bash", ["--noprofile", "--norc", "-euo", "pipefail", "-c", step.run], {
      cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../.."),
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}${path.delimiter}${process.env.PATH}`,
        ARCH: "amd64",
        TEST_IMAGE: "justup:test-amd64",
        PLATFORM: "linux/amd64",
        RUNNER_TEMP: temp,
        PYTHON_CALLS: calls,
      },
    });

    assert.equal(result.status, 42, `the verifier's exit status should propagate: ${result.stdout}\n${result.stderr}`);
    assert.match(readFileSync(calls, "utf8"), /tools\/verify-runtime-image\.py layers/);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});