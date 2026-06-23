import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { join, normalize, relative } from "node:path";

const RESET = "\x1b[0m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const ORANGE = "\x1b[38;5;208m";
const MAUVE = "\x1b[38;5;96m";
const TEAL = "\x1b[38;5;66m";
const TAN = "\x1b[38;5;137m";
const STEEL = "\x1b[38;5;67m";
const DIM = "\x1b[38;5;245m";
const CACHE_READ = "\x1b[38;5;79m";
const MODEL_OPUS = "\x1b[35m";
const MODEL_SONNET = "\x1b[36m";
const MODEL_HAIKU = "\x1b[34m";
const MODEL_FABLE = "\x1b[32m";
const SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";

export interface RenderState {
	turnActive: boolean;
	turnStart: number | undefined;
	turnIndex: number;
	lastProviderStatus: number | undefined;
	lastHeaders: Record<string, string>;
	spendCache: Map<string, { computedAt: number; spend: number }>;
	gitCache: { cwd: string; computedAt: number; value: string } | undefined;
	diffCache: { cwd: string; computedAt: number; value: string } | undefined;
}

interface Totals {
	input: number;
	cacheRead: number;
	cacheWrite: number;
	output: number;
	inputCost: number;
	cacheReadCost: number;
	cacheWriteCost: number;
	outputCost: number;
	totalCost: number;
	turns: number;
	lastModel: string;
	ttlEvictions: number;
	ttlWasted: number;
}

function color(text: string, ansi: string): string {
	return `${ansi}${text}${RESET}`;
}

function visibleWidth(text: string): number {
	return text.replace(/\x1b\[[0-9;]*m/g, "").length;
}

function truncateToWidth(text: string, width: number): string {
	if (visibleWidth(text) <= width) return text;
	let result = "";
	let visible = 0;
	for (let index = 0; index < text.length && visible < Math.max(0, width - 1); index++) {
		if (text[index] === "\x1b") {
			const match = text.slice(index).match(/^\x1b\[[0-9;]*m/);
			if (match) {
				result += match[0];
				index += match[0].length - 1;
				continue;
			}
		}
		result += text[index];
		visible += 1;
	}
	return `${result}…${RESET}`;
}

function humanNumber(value: number): string {
	const absolute = Math.abs(value);
	if (absolute >= 1_000_000) return `${(value / 1_000_000).toFixed(absolute >= 10_000_000 ? 1 : 2)}M`;
	if (absolute >= 1_000) return `${(value / 1_000).toFixed(absolute >= 100_000 ? 0 : 1)}K`;
	return `${Math.round(value)}`;
}

function money(value: number): string {
	return `$${value.toFixed(value < 10 ? 2 : 1)}`;
}

function highBadColor(percent: number, yellowAt: number, redAt: number): string {
	if (percent >= redAt) return RED;
	if (percent >= yellowAt) return YELLOW;
	return GREEN;
}

function highGoodColor(percent: number, greenAt: number, redAt: number): string {
	if (percent >= greenAt) return GREEN;
	if (percent <= redAt) return RED;
	return YELLOW;
}

function formatDuration(milliseconds: number): string {
	const seconds = Math.max(0, Math.floor(milliseconds / 1000));
	const hours = Math.floor(seconds / 3600);
	const minutes = Math.floor((seconds % 3600) / 60);
	const remainingSeconds = seconds % 60;
	if (hours > 0) return `${hours}h${String(minutes).padStart(2, "0")}m`;
	if (minutes > 0) return `${minutes}m${String(remainingSeconds).padStart(2, "0")}s`;
	return `${remainingSeconds}s`;
}

function modelBadge(modelId: string | undefined): string {
	const id = (modelId ?? "").toLowerCase();
	if (!id) return "";
	const specs: Array<[string, string]> = [
		["opus", MODEL_OPUS],
		["sonnet", MODEL_SONNET],
		["haiku", MODEL_HAIKU],
		["fable", MODEL_FABLE],
		["gpt", MODEL_SONNET],
		["gemini", MODEL_HAIKU],
	];
	for (const [family, ansi] of specs) {
		if (!id.includes(family)) continue;
		const version = id.match(new RegExp(`${family}[-.]?(\\d+)(?:[-.](\\d+))?`));
		const suffix = id.includes("1m") || id.includes("1000000") ? "[1m]" : "";
		const body = version ? `${family}${version[1]}${version[2] ? `.${version[2]}` : ""}${suffix}` : `${family}${suffix}`;
		return color(body, ansi);
	}
	return color((modelId ?? "").replace(/^claude-/, "").replace(/^openai\//, ""), DIM);
}

function timestampMs(value: string | number | undefined): number | undefined {
	if (typeof value === "number") return value;
	if (typeof value !== "string") return undefined;
	const parsed = Date.parse(value);
	return Number.isFinite(parsed) ? parsed : undefined;
}

function latestContextEstimate(ctx: any): number {
	for (const entry of [...ctx.sessionManager.getBranch()].reverse()) {
		if (entry.type !== "message" || entry.message.role !== "assistant") continue;
		const usage = entry.message.usage;
		return (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
	}
	return 0;
}

function contextSummary(ctx: any): string {
	const usage = ctx.getContextUsage();
	const windowSize = usage?.contextWindow ?? ctx.model?.contextWindow ?? 0;
	const tokens = usage?.tokens ?? latestContextEstimate(ctx);
	if (!tokens || !windowSize) return "";
	const percent = (tokens / windowSize) * 100;
	const ansi = highBadColor(percent, windowSize >= 1_000_000 ? 50 : 60, 85);
	return `${color(humanNumber(tokens), ansi)} / ${color(humanNumber(windowSize), MAUVE)} (${color(`${percent.toFixed(1)}%`, ansi)})`;
}

function cacheTtlSeconds(usage: any): number {
	const oneHour = usage.cacheWrite1h ?? 0;
	const fiveMinute = Math.max(0, (usage.cacheWrite ?? 0) - oneHour);
	if (oneHour || fiveMinute) return oneHour >= fiveMinute ? 3600 : 300;
	return 3600;
}

function totalsFromBranch(ctx: any): Totals {
	const totals: Totals = { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, inputCost: 0, cacheReadCost: 0, cacheWriteCost: 0, outputCost: 0, totalCost: 0, turns: 0, lastModel: ctx.model?.id ?? "", ttlEvictions: 0, ttlWasted: 0 };
	let previousTimestamp: number | undefined;
	let previousTtlSeconds = 3600;
	for (const entry of ctx.sessionManager.getBranch()) {
		if (entry.type !== "message" || entry.message.role !== "assistant") continue;
		const message = entry.message;
		const usage = message.usage;
		totals.turns += 1;
		totals.input += usage.input ?? 0;
		totals.cacheRead += usage.cacheRead ?? 0;
		totals.cacheWrite += usage.cacheWrite ?? 0;
		totals.output += usage.output ?? 0;
		totals.inputCost += usage.cost?.input ?? 0;
		totals.cacheReadCost += usage.cost?.cacheRead ?? 0;
		totals.cacheWriteCost += usage.cost?.cacheWrite ?? 0;
		totals.outputCost += usage.cost?.output ?? 0;
		totals.totalCost += usage.cost?.total ?? 0;
		totals.lastModel = message.responseModel ?? message.model ?? totals.lastModel;
		const currentTimestamp = timestampMs(entry.timestamp) ?? message.timestamp;
		const idleGapExceeded = previousTimestamp !== undefined && currentTimestamp !== undefined && (currentTimestamp - previousTimestamp) / 1000 > previousTtlSeconds;
		if (totals.turns > 1 && (usage.cacheRead ?? 0) === 0 && (usage.cacheWrite ?? 0) >= 1000 && idleGapExceeded) {
			totals.ttlEvictions += 1;
			totals.ttlWasted += (usage.cacheWrite ?? 0) * (ctx.model?.cost.input ?? 3) * 1.15 / 1_000_000;
		}
		previousTimestamp = currentTimestamp;
		previousTtlSeconds = cacheTtlSeconds(usage);
	}
	return totals;
}

function cacheSummary(totals: Totals, verbose: boolean): string {
	const denominator = totals.input + totals.cacheRead;
	if (!denominator) return "";
	const hit = (totals.cacheRead / denominator) * 100;
	const hitText = color(`${hit.toFixed(0)}% hit`, highGoodColor(hit, 90, 75));
	const read = `${color(humanNumber(totals.cacheRead), CACHE_READ)} ${color(`(${money(totals.cacheReadCost)})`, CACHE_READ)}`;
	const write = `${color(humanNumber(totals.cacheWrite), ORANGE)} ${color(`(${money(totals.cacheWriteCost)})`, ORANGE)}`;
	if (!verbose) return `${read} / ${write} / ${hitText}`;
	const input = `${color(humanNumber(totals.input), STEEL)} ${color(`(${money(totals.inputCost)})`, STEEL)}`;
	const output = `${color(humanNumber(totals.output), MODEL_OPUS)} ${color(`(${money(totals.outputCost)})`, MODEL_OPUS)}`;
	return `${input} / ${read} / ${write} / ${output} / ${hitText}`;
}

function ttlSummary(totals: Totals): string {
	return totals.ttlEvictions ? `${RED}⚠ TTL:${totals.ttlEvictions} (~${money(totals.ttlWasted)})${RESET}` : "";
}

function costSummary(totals: Totals): string {
	if (totals.totalCost <= 0) return "";
	return color(money(totals.totalCost), totals.totalCost >= 70 ? RED : totals.totalCost >= 35 ? YELLOW : GREEN);
}

function spendSince(windowMilliseconds: number, state: RenderState): number {
	const key = String(windowMilliseconds);
	const cached = state.spendCache.get(key);
	const now = Date.now();
	if (cached && now - cached.computedAt < 15_000) return cached.spend;
	const windowStart = now - windowMilliseconds;
	let spend = 0;
	try {
		for (const project of readdirSync(join(homedir(), ".pi", "agent", "sessions"))) {
			const projectPath = join(homedir(), ".pi", "agent", "sessions", project);
			if (!statSync(projectPath).isDirectory()) continue;
			for (const file of readdirSync(projectPath)) {
				if (!file.endsWith(".jsonl")) continue;
				const path = join(projectPath, file);
				if (statSync(path).mtimeMs >= windowStart) spend += spendFromFile(path, windowStart);
			}
		}
	} catch {
		spend = 0;
	}
	state.spendCache.set(key, { computedAt: now, spend });
	return spend;
}

function spendFromFile(path: string, windowStart: number): number {
	try {
		return readFileSync(path, "utf8").split(/\r?\n/).reduce((sum, line) => {
			if (!line.trim()) return sum;
			try {
				const entry = JSON.parse(line);
				if (entry.type !== "message" || entry.message?.role !== "assistant") return sum;
				const timestamp = timestampMs(entry.timestamp) ?? entry.message.timestamp;
				return timestamp && timestamp >= windowStart ? sum + (entry.message.usage?.cost?.total ?? 0) : sum;
			} catch {
				return sum;
			}
		}, 0);
	} catch {
		return 0;
	}
}

function dayBudgetSummary(spend: number): string {
	const budget = Number(process.env.STATUSLINE_DAILY_BUDGET ?? "0");
	if (!Number.isFinite(budget) || budget <= 0) return "";
	const percent = (spend / budget) * 100;
	return `day: ${color(`${percent.toFixed(0)}%`, highBadColor(percent, 75, 90))}`;
}

function burnRateSummary(rate: number, spend24h: number): string {
	if (rate <= 0 && !process.env.STATUSLINE_DAILY_BUDGET) return "";
	const target = Number(process.env.STATUSLINE_TARGET_RATE ?? "1");
	const usableTarget = Number.isFinite(target) && target > 0 ? target : 1;
	const ratio = rate / usableTarget;
	const ansi = ratio >= 4 ? RED : ratio >= 1.5 ? YELLOW : ratio < 0.5 ? CACHE_READ : GREEN;
	let needle = "";
	const budget = Number(process.env.STATUSLINE_DAILY_BUDGET ?? "0");
	if (budget > 0 && spend24h > 0) {
		const budgetRatio = spend24h / budget;
		needle = budgetRatio > 1.05 ? color("↑", highBadColor(budgetRatio * 100, 150, 300)) : budgetRatio < 0.95 ? color("↓", GREEN) : color("☯︎", GREEN);
	}
	return `${color(`$${rate.toFixed(2)}/min`, ansi)} ${color(`→$${usableTarget.toFixed(2)}`, GREEN)}${needle ? ` ${needle}` : ""}`;
}

function rateLimitSummary(state: RenderState): string {
	const headers = state.lastHeaders;
	const requestRemaining = headers["x-ratelimit-remaining-requests"] ?? headers["anthropic-ratelimit-requests-remaining"];
	const tokenRemaining = headers["x-ratelimit-remaining-tokens"] ?? headers["anthropic-ratelimit-tokens-remaining"] ?? headers["anthropic-ratelimit-input-tokens-remaining"];
	const parts = [];
	if (requestRemaining) parts.push(`req:${requestRemaining}`);
	if (tokenRemaining) parts.push(`tok:${humanNumber(Number(tokenRemaining) || 0)}`);
	return parts.length ? color(`rl ${parts.join(" ")}`, DIM) : "";
}

function gitRef(cwd: string, state: RenderState): string {
	const now = Date.now();
	if (state.gitCache && state.gitCache.cwd === cwd && now - state.gitCache.computedAt < 5000) return state.gitCache.value;
	let value = "";
	try {
		const branch = execFileSync("git", ["-C", cwd, "symbolic-ref", "--short", "HEAD"], { encoding: "utf8", timeout: 1000 }).trim();
		const hash = execFileSync("git", ["-C", cwd, "rev-parse", "--short", "HEAD"], { encoding: "utf8", timeout: 1000 }).trim();
		value = branch && hash ? `${branch}:${color(hash, TAN)}` : branch || color(hash, TAN);
	} catch {
		value = "";
	}
	state.gitCache = { cwd, computedAt: now, value };
	return value;
}

function diffStat(cwd: string, state: RenderState): string {
	const now = Date.now();
	if (state.diffCache && state.diffCache.cwd === cwd && now - state.diffCache.computedAt < 2000) return state.diffCache.value;
	let added = 0;
	let removed = 0;
	try {
		for (const line of execFileSync("git", ["-C", cwd, "diff", "--numstat"], { encoding: "utf8", timeout: 1000 }).trim().split(/\r?\n/)) {
			if (!line) continue;
			const [add, remove] = line.split(/\s+/);
			added += Number(add) || 0;
			removed += Number(remove) || 0;
		}
	} catch {
		added = 0;
		removed = 0;
	}
	state.diffCache = { cwd, computedAt: now, value: added || removed ? `${color(`+${added}`, GREEN)}/${color(`-${removed}`, RED)}` : "" };
	return state.diffCache.value;
}

function formatCwd(home: string, current: string): string {
	if (!home || normalize(home).toLowerCase() === normalize(current).toLowerCase()) return current || home;
	const hop = relative(home, current);
	const display = hop && !hop.startsWith("..") && !hop.includes(":") ? `./${hop}` : current;
	return `${home} ${color(`[${display}]`, TEAL)}`;
}

function line1(ctx: any, state: RenderState): string {
	const header = ctx.sessionManager.getHeader();
	let line = `${SPINNER_FRAMES[Math.floor(Date.now() / 250) % SPINNER_FRAMES.length]} [${color(hostname().split(".")[0] || "unknown", MAUVE)}] ${formatCwd(header?.cwd ?? ctx.cwd, ctx.cwd)}`;
	const ref = gitRef(ctx.cwd, state);
	if (ref) line += ` (${ref})`;
	const sessionId = ctx.sessionManager.getSessionId();
	if (sessionId) line += ` ${color(`[${sessionId.slice(0, 8)}]`, STEEL)}`;
	const name = ctx.sessionManager.getSessionName();
	if (name) line += ` ${color(name.length > 58 ? `${name.slice(0, 57)}…` : name, DIM)}`;
	return line;
}

function line2(ctx: any, state: RenderState, width: number): string {
	const totals = totalsFromBranch(ctx);
	const spend5m = spendSince(300_000, state);
	const spend24h = spendSince(86_400_000, state);
	return [modelBadge(totals.lastModel || ctx.model?.id), contextSummary(ctx), cacheSummary(totals, width >= 150), ttlSummary(totals), rateLimitSummary(state), dayBudgetSummary(spend24h), burnRateSummary(spend5m / 5, spend24h), costSummary(totals), diffStat(ctx.cwd, state)].filter(Boolean).join(" | ");
}

function line3(ctx: any, state: RenderState): string {
	const parts = [];
	const headerTimestamp = timestampMs(ctx.sessionManager.getHeader()?.timestamp);
	if (headerTimestamp) parts.push(`⏳ ${formatDuration(Date.now() - headerTimestamp)}`);
	if (state.turnActive && state.turnStart) parts.push(`⏱ turn ${state.turnIndex} (${formatDuration(Date.now() - state.turnStart)})`);
	if (state.lastProviderStatus) parts.push(color(`http ${state.lastProviderStatus}`, state.lastProviderStatus >= 400 ? RED : DIM));
	return parts.join(" · ");
}

export function installStatuslineFooter(ctx: any, state: RenderState): void {
	ctx.ui.setFooter((tui: any, _theme: any, footerData: any) => {
		const unsub = footerData.onBranchChange(() => tui.requestRender());
		const interval = setInterval(() => {
			if (state.turnActive) tui.requestRender();
		}, 1000);
		return {
			dispose() {
				unsub();
				clearInterval(interval);
			},
			invalidate() {},
			render(width: number): string[] {
				return [line1(ctx, state), line2(ctx, state, width), line3(ctx, state)].filter(Boolean).map((line) => truncateToWidth(line, width));
			},
		};
	});
}
