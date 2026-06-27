import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { dirname, join, normalize, relative } from "node:path";

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
const SPEND_WINDOW_MS_5M = 300_000;
const SPEND_WINDOW_MS_24H = 86_400_000;
const SPEND_CACHE_TTL_MS = 15_000;

export interface RenderState {
	turnActive: boolean;
	turnStart: number | undefined;
	turnIndex: number;
	lastProviderStatus: number | undefined;
	lastHeaders: Record<string, string>;
	spendCache: Map<string, { computedAt: number; spend: number; sourcePath?: string }>;
	gitCache: { cwd: string; computedAt: number; value: string } | undefined;
	diffCache: { cwd: string; computedAt: number; value: string } | undefined;
	gitRepoCache: { cwd: string; computedAt: number; value: boolean } | undefined;
	totalsCache: CachedTotals | undefined;
	branchSpendCache: BranchSpendCache | undefined;
	spendSessionPath: string | undefined;
	spendSessionId: string | undefined;
}

interface BranchSpendCache {
	sessionId: string;
	fingerprint: string;
	computedAt: number;
	spendByWindow: Record<number, number>;
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
	currentContextTokens: number;
}

interface CachedTotals {
	fingerprint: string;
	value: Totals;
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

function branchFingerprint(branch: any[]): string {
	const count = branch.length;
	if (count === 0) return "0";

	for (let index = branch.length - 1; index >= 0; index--) {
		const entry = branch[index];
		if (entry?.type !== "message" || entry.message?.role !== "assistant") continue;
		const usage = entry.message.usage ?? {};
		const model = entry.message.responseModel ?? entry.message.model ?? "";
		const currentTimestamp = timestampMs(entry.timestamp) ?? entry.message.timestamp;
		return `${count}|${index}|${currentTimestamp ?? ""}|${model}|${usage.input ?? 0}|${usage.cacheRead ?? 0}|${usage.cacheWrite ?? 0}|${usage.output ?? 0}|${usage.cost?.total ?? 0}`;
	}

	return String(count);
}

function totalsFromBranch(branch: any[]): Totals {
	const totals: Totals = {
		input: 0,
		cacheRead: 0,
		cacheWrite: 0,
		output: 0,
		inputCost: 0,
		cacheReadCost: 0,
		cacheWriteCost: 0,
		outputCost: 0,
		totalCost: 0,
		turns: 0,
		lastModel: "",
		ttlEvictions: 0,
		ttlWasted: 0,
		currentContextTokens: 0,
	};
	let previousTimestamp: number | undefined;
	let previousTtlSeconds = 3600;
	for (const entry of branch) {
		if (entry.type !== "message" || entry.message.role !== "assistant") continue;
		const message = entry.message;
		const usage = message.usage ?? {};
		totals.turns += 1;
		totals.input += usage.input ?? 0;
		totals.cacheRead += usage.cacheRead ?? 0;
		totals.cacheWrite += usage.cacheWrite ?? 0;
		totals.output += usage.output ?? 0;
		totals.currentContextTokens = (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
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
			totals.ttlWasted +=
				(usage.cacheWrite ?? 0) *
				inputRatePerMillion(message.responseModel ?? message.model) *
				1.15 /
				1_000_000;
		}
		previousTimestamp = currentTimestamp;
		previousTtlSeconds = cacheTtlSeconds(usage);
	}
	if (!totals.lastModel) totals.lastModel = branch?.at?.(-1)?.message?.responseModel ?? branch?.at?.(-1)?.message?.model ?? "";
	return totals;
}

function branchTotals(ctx: any, state: RenderState): Totals {
	const branch = ctx.sessionManager.getBranch();
	const fingerprint = branchFingerprint(branch);
	if (state.totalsCache?.fingerprint === fingerprint) {
		return state.totalsCache.value;
	}
	const totals = totalsFromBranch(branch);
	state.totalsCache = { fingerprint, value: totals };
	return totals;
}

function spendFromBranchWindows(branch: any[], now: number): Record<number, number> {
	const spendByWindow: Record<number, number> = {
		[SPEND_WINDOW_MS_5M]: 0,
		[SPEND_WINDOW_MS_24H]: 0,
	};
	const window5mStart = now - SPEND_WINDOW_MS_5M;
	const window24hStart = now - SPEND_WINDOW_MS_24H;
	for (const entry of branch) {
		if (entry.type !== "message" || entry.message?.role !== "assistant") continue;
		const ts = timestampMs(entry.timestamp) ?? timestampMs(entry.message.timestamp);
		if (ts === undefined) continue;
		const rawCost = entry.message?.usage?.cost?.total;
		const totalCost = typeof rawCost === "number" ? rawCost : Number(rawCost);
		if (!Number.isFinite(totalCost) || totalCost <= 0) continue;
		if (ts >= window24hStart) {
			spendByWindow[SPEND_WINDOW_MS_24H] += totalCost;
			if (ts >= window5mStart) {
				spendByWindow[SPEND_WINDOW_MS_5M] += totalCost;
			}
		}
	}
	return spendByWindow;
}

function spendByWindowFromBranch(ctx: any, state: RenderState, sessionId: string | undefined, now: number): Record<number, number> | undefined {
	if (!sessionId) return undefined;
	const branch = ctx.sessionManager.getBranch() || [];
	if (!branch.length) return undefined;
	const fingerprint = branchFingerprint(branch);
	const cached = state.branchSpendCache;
	if (cached && cached.sessionId === sessionId && cached.fingerprint === fingerprint && now - cached.computedAt < SPEND_CACHE_TTL_MS && cached.spendByWindow[SPEND_WINDOW_MS_5M] !== undefined && cached.spendByWindow[SPEND_WINDOW_MS_24H] !== undefined) {
		return cached.spendByWindow;
	}
	const spendByWindow = spendFromBranchWindows(branch, now);
	state.branchSpendCache = {
		sessionId,
		fingerprint,
		computedAt: now,
		spendByWindow,
	};
	return spendByWindow;
}

function contextSummary(ctx: any, totals?: Totals): string {
	const usage = ctx.getContextUsage();
	const windowSize = usage?.contextWindow ?? ctx.model?.contextWindow ?? 0;
	const tokens = usage?.tokens ?? totals?.currentContextTokens ?? 0;
	if (!tokens || !windowSize) return "";
	const percent = (tokens / windowSize) * 100;
	const ansi = highBadColor(percent, windowSize >= 1_000_000 ? 50 : 60, 85);
	return `${color(humanNumber(tokens), ansi)} / ${color(humanNumber(windowSize), MAUVE)} (${color(`${percent.toFixed(1)}%`, ansi)})`;
}

function resolveSessionTranscriptPath(sessionId: string | undefined, state: RenderState): string | undefined {
	if (!sessionId) return undefined;
	if (state.spendSessionId === sessionId && state.spendSessionPath) return state.spendSessionPath;
	const sessionsRoot = join(homedir(), ".pi", "agent", "sessions");
	try {
		for (const project of readdirSync(sessionsRoot)) {
			const projectPath = join(sessionsRoot, project);
			if (!statSync(projectPath).isDirectory()) continue;
			for (const file of readdirSync(projectPath)) {
				if (!file.endsWith(".jsonl") || !file.includes(sessionId)) continue;
				const sessionPath = join(projectPath, file);
				state.spendSessionPath = sessionPath;
				state.spendSessionId = sessionId;
				return sessionPath;
			}
		}
	} catch {
		// fall through to global scan fallback
	}
	state.spendSessionPath = undefined;
	state.spendSessionId = sessionId;
	return undefined;
}

function spendFromAllSessions(windowStart: number): number {
	let spend = 0;
	try {
		for (const project of readdirSync(join(homedir(), ".pi", "agent", "sessions"))) {
			const projectPath = join(homedir(), ".pi", "agent", "sessions", project);
			if (!statSync(projectPath).isDirectory()) continue;
			for (const file of readdirSync(projectPath)) {
				if (!file.endsWith(".jsonl")) continue;
				const path = join(projectPath, file);
				if (statSync(path).mtimeMs >= windowStart) {
					spend += spendFromFile(path, windowStart);
				}
			}
		}
	} catch {
		spend = 0;
	}
	return spend;
}

function inputRatePerMillion(modelId: string | undefined): number {
	const lowered = (modelId ?? "").toLowerCase();
	if (lowered.includes("fable")) return 10;
	if (lowered.includes("opus")) return 5;
	if (lowered.includes("sonnet")) return 3;
	if (lowered.includes("haiku")) return 1;
	return 3;
}

function cacheTtlSeconds(usage: any): number {
	const oneHour = usage.cacheWrite1h ?? 0;
	const fiveMinute = Math.max(0, (usage.cacheWrite ?? 0) - oneHour);
	if (oneHour || fiveMinute) return oneHour >= fiveMinute ? 3600 : 300;
	return 3600;
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

function spendSince(windowMilliseconds: number, state: RenderState, sessionId: string | undefined, ctx?: any): number {
	const now = Date.now();
	const branchSpend = spendByWindowFromBranch(ctx, state, sessionId, now);
	if (branchSpend && branchSpend[windowMilliseconds] !== undefined) {
		return branchSpend[windowMilliseconds];
	}
	const key = String(windowMilliseconds);
	const sourcePath = resolveSessionTranscriptPath(sessionId, state);
	const cached = state.spendCache.get(key);
	if (cached && now - cached.computedAt < SPEND_CACHE_TTL_MS && cached.sourcePath === sourcePath) {
		return cached.spend;
	}
	const windowStart = now - windowMilliseconds;
	const spend = sourcePath ? spendFromFile(sourcePath, windowStart) : spendFromAllSessions(windowStart);
	state.spendCache.set(key, { computedAt: now, spend, sourcePath });
	return spend;
}

function spendFromFile(path: string, windowStart: number): number {
	try {
		return readFileSync(path, "utf8").split(/\r?\n/).reduce((sum, line) => {
			if (!line.trim()) return sum;
			try {
				const entry = JSON.parse(line);
				if (entry.type !== "message" || entry.message?.role !== "assistant") return sum;
				const timestamp = timestampMs(entry.timestamp) ?? timestampMs(entry.message?.timestamp);
				const rawCost = entry.message?.usage?.cost?.total;
				const cost = typeof rawCost === "number" ? rawCost : Number(rawCost);
				return timestamp && timestamp >= windowStart && Number.isFinite(cost) ? sum + cost : sum;
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

function isGitRepository(cwd: string, state: RenderState): boolean {
	const now = Date.now();
	const cached = state.gitRepoCache;
	if (cached && cached.cwd === cwd && now - cached.computedAt < 5000) return cached.value;

	let current = cwd;
	while (true) {
		if (existsSync(join(current, ".git"))) {
			try {
				const stat = statSync(join(current, ".git"));
				if (stat.isDirectory() || stat.isFile()) {
					state.gitRepoCache = { cwd, computedAt: now, value: true };
					return true;
				}
			} catch {
				// fall through
			}
		}
		const parent = dirname(current);
		if (parent === current) break;
		current = parent;
	}
	state.gitRepoCache = { cwd, computedAt: now, value: false };
	return false;
}

function gitRef(cwd: string, state: RenderState): string {
	const now = Date.now();
	if (!isGitRepository(cwd, state)) {
		state.gitCache = { cwd, computedAt: now, value: "" };
		return "";
	}
	if (state.gitCache && state.gitCache.cwd === cwd && now - state.gitCache.computedAt < 8000) return state.gitCache.value;
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
	if (!isGitRepository(cwd, state)) {
		state.diffCache = { cwd, computedAt: now, value: "" };
		return "";
	}
	if (state.diffCache && state.diffCache.cwd === cwd && now - state.diffCache.computedAt < 5000) return state.diffCache.value;
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
	const totals = branchTotals(ctx, state);
	const sessionId = ctx.sessionManager.getSessionId();
	const spend5m = spendSince(SPEND_WINDOW_MS_5M, state, sessionId, ctx);
	const spend24h = spendSince(SPEND_WINDOW_MS_24H, state, sessionId, ctx);
	return [
		modelBadge(totals.lastModel || ctx.model?.id),
		contextSummary(ctx, totals),
		cacheSummary(totals, width >= 150),
		ttlSummary(totals),
		rateLimitSummary(state),
		dayBudgetSummary(spend24h),
		burnRateSummary(spend5m / 5, spend24h),
		costSummary(totals),
		diffStat(ctx.cwd, state),
	].filter(Boolean).join(" | ");
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
		const unsub = footerData.onBranchChange(() => {
			state.totalsCache = undefined;
			state.branchSpendCache = undefined;
			tui.requestRender();
		});
		const interval = setInterval(() => {
			if (state.turnActive) tui.requestRender();
		}, 1500);
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
