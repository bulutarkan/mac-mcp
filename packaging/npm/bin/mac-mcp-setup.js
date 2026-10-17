#!/usr/bin/env node
// Mac MCP's npm entry point. It installs Mac MCP with the installer shipped in this package
// (the same install.sh as the GitHub repository, which verifies the signed stable release before
// it installs anything) and hands every other command to the installed `mac-mcp` CLI.
// Nothing runs when the package is installed: there are no npm lifecycle scripts.
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PACKAGE = require("../package.json");
const INSTALLER = path.join(__dirname, "..", "install.sh");
const PASSED_THROUGH = new Set(["update", "status", "start", "stop", "restart", "doctor", "logs", "connect", "clients",
  "repair", "uninstall"]);

function cliPath() {
  // The installer's own default (MAC_MCP_BIN_DIR or ~/.local/bin); never a `mac-mcp` found on PATH,
  // which could be this package's own command and recurse.
  const binDir = process.env.MAC_MCP_BIN_DIR || path.join(os.homedir(), ".local", "bin");
  return path.join(binDir, "mac-mcp");
}

function say(text) {
  process.stdout.write(`${text}\n`);
}

function fail(text, code = 1) {
  process.stderr.write(`mac-mcp-setup: ${text}\n`);
  process.exit(code);
}

function help() {
  say(`Mac MCP ${PACKAGE.version}: let your AI apps use your Mac.

Usage:
  npx @bulutarkan/mac-mcp install     Install Mac MCP (server, CLI and menu bar app)
  npx @bulutarkan/mac-mcp update      Update an existing install (verified stable releases only)
  npx @bulutarkan/mac-mcp status      Show whether Mac MCP is running
  npx @bulutarkan/mac-mcp <command>   start, stop, restart, doctor, logs, connect, clients, repair
                                      and uninstall also work

The installer checks the signed Mac MCP release before installing, never overwrites an existing
install, settings or keys, and asks before optional extras. Once installed, the 'mac-mcp' command
does the same as these commands. More: https://github.com/bulutarkan/mac-mcp`);
}

function checkMac() {
  if (process.platform !== "darwin") fail("Mac MCP runs on macOS only.");
  const darwinMajor = Number(os.release().split(".")[0]);
  if (!(darwinMajor >= 22)) fail("Mac MCP needs macOS 13 (Ventura) or later.");
}

function install(args) {
  checkMac();
  if (fs.existsSync(cliPath())) {
    say(`Mac MCP is already installed (${cliPath()}). Nothing was changed.`);
    say("To update it: npx @bulutarkan/mac-mcp update   (or: mac-mcp update)");
    return 0;
  }
  if (!fs.existsSync(INSTALLER)) fail("The installer is missing from this package; reinstall @bulutarkan/mac-mcp.");
  const run = spawnSync("/bin/bash", [INSTALLER, ...args], { stdio: "inherit", cwd: os.homedir(), env: process.env });
  if (run.error) fail(`Could not start the installer: ${run.error.message}`);
  return run.status === null ? 1 : run.status;
}

function passThrough(command, args) {
  checkMac();
  const cli = cliPath();
  if (!fs.existsSync(cli)) fail("Mac MCP is not installed yet. Run: npx @bulutarkan/mac-mcp install");
  if (fs.realpathSync(cli) === fs.realpathSync(__filename)) fail(`${cli} points back to this package; refusing to recurse.`);
  const run = spawnSync(cli, [command, ...args], { stdio: "inherit", env: process.env });
  if (run.error) fail(`Could not run ${cli}: ${run.error.message}`);
  return run.status === null ? 1 : run.status;
}

function main(argv) {
  const [command, ...rest] = argv;
  if (!command || command === "help" || command === "--help" || command === "-h") {
    help();
    return 0;
  }
  if (command === "--version" || command === "-v" || command === "version") {
    say(PACKAGE.version);
    return 0;
  }
  if (command === "install") return install(rest);
  if (PASSED_THROUGH.has(command)) return passThrough(command, rest);
  process.stderr.write(`mac-mcp-setup: unknown command '${command}'.\n\n`);
  help();
  return 2;
}

process.exit(main(process.argv.slice(2)));
