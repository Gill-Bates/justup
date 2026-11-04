//
// app/static/js/utils/optional-groups.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The one mechanic for optional field groups (progressive disclosure, DESIGN.md "Optional field groups").
// A hidden group leaves layout, tab order and the accessibility tree; `disabled` alone would not.
(function (root) {
	'use strict';

	// Show or hide every `[data-<attribute>]` element of `container` by its group key.
	function apply(container, attribute, visible) {
		const key = attribute.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
		for (const element of container.querySelectorAll('[data-' + attribute + ']')) {
			element.hidden = !visible[element.dataset[key]];
		}
		return visible;
	}

	// A switch with aria-controls owns the group(s) it names: on shows them, off hides them.
	// Returns { groupId: visible }. Focus that sat inside a group being hidden moves to its switch.
	function syncSwitches(scope) {
		const visible = {};
		for (const toggle of scope.querySelectorAll('input[role="switch"][aria-controls]')) {
			for (const id of toggle.getAttribute('aria-controls').split(/\s+/).filter(Boolean)) {
				const group = scope.querySelector('[id="' + id + '"]');
				if (!group) continue;
				visible[id] = toggle.checked;
				group.hidden = !toggle.checked;
				if (group.hidden && group.contains(root.document.activeElement)) toggle.focus();
			}
		}
		return visible;
	}

	root.JuOptionalGroups = { apply: apply, syncSwitches: syncSwitches };
})(window);
