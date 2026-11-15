//
// tests/layout/page-errors.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Shared page-error collector for the layout specs.
// WebKit reports a request of the live client (EventSource /ui/events, probe /health) that a navigation or reload cancels as a
// page error "... cannot load <loopback>/<endpoint> due to access control checks". It is no app fault and no CORS problem: same origin.
const WEBKIT_ABORTED_LIVE_REQUEST = /^(EventSource|Fetch API) cannot load https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?\/(ui\/events|health)\/?(\?\S*)? due to access control checks\.?$/;

export const isAbortedLiveRequest = (error) => WEBKIT_ABORTED_LIVE_REQUEST.test(String(error?.message ?? error).trim());

// Pushes every page error except the known WebKit abort message into `sink`; `format` keeps a spec's own text form.
export function collectPageErrors(page, sink, format = String) {
	page.on("pageerror", (error) => { if (!isAbortedLiveRequest(error)) sink.push(format(error)); });
	return sink;
}
