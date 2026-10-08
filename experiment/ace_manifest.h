/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_MANIFEST_H
#define ACE_MANIFEST_H

/* Preparation-only provenance capture. condition_json must be a validated
 * JSON object containing parameters shared by repetitions (no run paths or
 * timestamps). output is a new manifest filename; an existing file is never
 * replaced. Returns 0 on publication, -1 with errno on failure. */
int ace_write_manifest(const char *output, const char *run_id,
		const char *condition_id, const char *interface,
		const char *repo, const char *condition_json);

#endif
