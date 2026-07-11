// Pi port of schoen-claude-status. Loaded by ~/.pi/agent/extensions/agent-statusline.

import { installStatuslineFooter, type RenderState } from "./renderer.ts";

export default function (pi: any) {
	const state: RenderState = {
		turnActive: false,
		turnStart: undefined,
		turnIndex: 0,
		lastProviderStatus: undefined,
		lastHeaders: {},
		spendCache: new Map(),
		gitCache: undefined,
		diffCache: undefined,
		gitRefreshPromise: undefined,
		requestRender: undefined,
		totalsCache: undefined,
		branchSpendCache: undefined,
		spendSessionPath: undefined,
		spendSessionId: undefined,
	};

	pi.on("session_start", async (_event: any, ctx: any) => {
		state.totalsCache = undefined;
		state.spendCache = new Map();
		state.spendSessionPath = undefined;
		state.spendSessionId = undefined;
		state.gitCache = undefined;
		state.diffCache = undefined;
		state.gitRefreshPromise = undefined;
		state.branchSpendCache = undefined;
		if (ctx.hasUI) installStatuslineFooter(ctx, state);
	});
	pi.on("turn_start", async (event: any) => {
		state.turnActive = true;
		state.turnStart = Date.now();
		state.turnIndex = (event.turnIndex ?? 0) + 1;
	});
	pi.on("turn_end", async () => {
		state.turnActive = false;
	});
	pi.on("after_provider_response", async (event: any) => {
		state.lastProviderStatus = event.status;
		state.lastHeaders = event.headers ?? {};
	});
	pi.on("session_shutdown", async (_event: any, ctx: any) => {
		ctx.ui.setFooter(undefined);
	});

	pi.registerCommand("statusline-pi", {
		description: "Reinstall the custom Pi statusline footer",
		handler: async (_args: string, ctx: any) => {
			installStatuslineFooter(ctx, state);
			ctx.ui.notify("Custom Pi statusline installed", "info");
		},
	});
}
