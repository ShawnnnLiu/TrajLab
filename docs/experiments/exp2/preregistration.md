# Exp 2 preregistration: `tb40-exp2`

Written 2026-10-28 07:39 UTC by `trajlab exp2 prereg` at `198ab1d537c395ed5562ded2a706cb6d0bc8a60b`.
It is committed before the first `record-engine` attempt runs: `trajlab exp2 run` refuses that arm until this file is the version at HEAD, and refuses any run whose prompts or derived views the code no longer reproduces (technical spec §5.3, ADR-0022).
The block under Settings is what the loop runs; the prose repeats it.

## Sample

56 failed first attempts of `corpus/jobs/tb40-sonnet-v4`, from 21 tasks; each roots one chain per arm. Store `corpus/stores/tb40-sonnet-v4`.

| Root failure | Task | Instruction sha256 |
|---|---|---|
| `bun-sourcemap-leak__3f2BwTC` | `terminal-bench/bun-sourcemap-leak` | `48524392be65b45b` |
| `bun-sourcemap-leak__X6KbUKo` | `terminal-bench/bun-sourcemap-leak` | `48524392be65b45b` |
| `bun-sourcemap-leak__b5DA76u` | `terminal-bench/bun-sourcemap-leak` | `48524392be65b45b` |
| `cad-model__Gun964v` | `terminal-bench/cad-model` | `354d74507e75e1fc` |
| `cad-model__MEwahRs` | `terminal-bench/cad-model` | `354d74507e75e1fc` |
| `cad-model__jhjUsFh` | `terminal-bench/cad-model` | `354d74507e75e1fc` |
| `cargo-flight-dispatch__DAgHbSz` | `terminal-bench/cargo-flight-dispatch` | `4c63f550eaee8759` |
| `cargo-flight-dispatch__FVgfyVQ` | `terminal-bench/cargo-flight-dispatch` | `4c63f550eaee8759` |
| `cargo-flight-dispatch__J8Ce4cR` | `terminal-bench/cargo-flight-dispatch` | `4c63f550eaee8759` |
| `foodstuff-beta-activity__2iHyDYM` | `terminal-bench/foodstuff-beta-activity` | `c54a479f56a22ac3` |
| `foodstuff-beta-activity__9XLzFHA` | `terminal-bench/foodstuff-beta-activity` | `c54a479f56a22ac3` |
| `foodstuff-beta-activity__XZsfWUj` | `terminal-bench/foodstuff-beta-activity` | `c54a479f56a22ac3` |
| `freecad-impeller__DddVQ9g` | `terminal-bench/freecad-impeller` | `2f0aebb2172e1530` |
| `freecad-impeller__GUqwwwf` | `terminal-bench/freecad-impeller` | `2f0aebb2172e1530` |
| `freecad-impeller__o7pg8wY` | `terminal-bench/freecad-impeller` | `2f0aebb2172e1530` |
| `freecad-platform-drawing__6tXhKU6` | `terminal-bench/freecad-platform-drawing` | `157b5310eca6c18d` |
| `freecad-platform-drawing__ibz96hK` | `terminal-bench/freecad-platform-drawing` | `157b5310eca6c18d` |
| `freecad-platform-drawing__yd3EsQP` | `terminal-bench/freecad-platform-drawing` | `157b5310eca6c18d` |
| `freecad-spring-clip__EmXRHs3` | `terminal-bench/freecad-spring-clip` | `5fbce9201a575f73` |
| `freecad-spring-clip__GHZZCFs` | `terminal-bench/freecad-spring-clip` | `5fbce9201a575f73` |
| `freecad-spring-clip__qnC44Cc` | `terminal-bench/freecad-spring-clip` | `5fbce9201a575f73` |
| `gsea-proteomics__jB5QsQj` | `terminal-bench/gsea-proteomics` | `ecc0cfc999d90256` |
| `gsea-proteomics__mqM8Mpm` | `terminal-bench/gsea-proteomics` | `ecc0cfc999d90256` |
| `gsea-proteomics__sqY9QmQ` | `terminal-bench/gsea-proteomics` | `ecc0cfc999d90256` |
| `interleaved-vigenere__N4Wfs8g` | `terminal-bench/interleaved-vigenere` | `58af12491e556be1` |
| `interleaved-vigenere__wMtCHHH` | `terminal-bench/interleaved-vigenere` | `58af12491e556be1` |
| `layout-config-recreation2__qGZer69` | `terminal-bench/layout-config-recreation2` | `5f3b38e66383c054` |
| `layout-config-recreation2__yq2Vt6U` | `terminal-bench/layout-config-recreation2` | `5f3b38e66383c054` |
| `layout-config-recreation__RN8QV4W` | `terminal-bench/layout-config-recreation` | `cdbc86ee8a1a9514` |
| `layout-config-recreation__RV5qQjA` | `terminal-bench/layout-config-recreation` | `cdbc86ee8a1a9514` |
| `layout-config-recreation__kb38wvM` | `terminal-bench/layout-config-recreation` | `cdbc86ee8a1a9514` |
| `mvcc-lsm-compaction__PFDAk9Y` | `terminal-bench/mvcc-lsm-compaction` | `a168992073c09912` |
| `mvcc-lsm-compaction__ccQd9vh` | `terminal-bench/mvcc-lsm-compaction` | `a168992073c09912` |
| `pretrain-shard-corruption__6egdDwc` | `terminal-bench/pretrain-shard-corruption` | `c3020127e6b5f235` |
| `pretrain-shard-corruption__Cgizn8v` | `terminal-bench/pretrain-shard-corruption` | `c3020127e6b5f235` |
| `pretrain-shard-corruption__R4QX4a7` | `terminal-bench/pretrain-shard-corruption` | `c3020127e6b5f235` |
| `production-planning__mXg4cWZ` | `terminal-bench/production-planning` | `27484dcd10cd4513` |
| `production-planning__un4NZWE` | `terminal-bench/production-planning` | `27484dcd10cd4513` |
| `production-planning__xU3cHDY` | `terminal-bench/production-planning` | `27484dcd10cd4513` |
| `protein-autointerp-disulfide__3KtgkCt` | `terminal-bench/protein-autointerp-disulfide` | `110ab0a67354908e` |
| `protein-autointerp-disulfide__eaT4bXV` | `terminal-bench/protein-autointerp-disulfide` | `110ab0a67354908e` |
| `protein-autointerp-disulfide__hetXe9M` | `terminal-bench/protein-autointerp-disulfide` | `110ab0a67354908e` |
| `roy-polymorph-cn__KVWqqpZ` | `terminal-bench/roy-polymorph-cn` | `1b0eaec23c28cd8a` |
| `roy-polymorph-cn__hrYnChi` | `terminal-bench/roy-polymorph-cn` | `1b0eaec23c28cd8a` |
| `roy-polymorph-cn__jizFeHm` | `terminal-bench/roy-polymorph-cn` | `1b0eaec23c28cd8a` |
| `sglang-qwen-burst__4z9SyjA` | `terminal-bench/sglang-qwen-burst` | `36108a78dee57649` |
| `sglang-qwen-burst__Y6ovEBk` | `terminal-bench/sglang-qwen-burst` | `36108a78dee57649` |
| `sglang-qwen-burst__exuuLAH` | `terminal-bench/sglang-qwen-burst` | `36108a78dee57649` |
| `sound-change-cascade__WGwuZ5g` | `terminal-bench/sound-change-cascade` | `255f6563177da56d` |
| `vba-userform-port__PFng8aw` | `terminal-bench/vba-userform-port` | `c68538f5ede995de` |
| `vf2-speedup-networkx__UVeaJc9` | `terminal-bench/vf2-speedup-networkx` | `3e85988fada1640e` |
| `vf2-speedup-networkx__bDWTHvP` | `terminal-bench/vf2-speedup-networkx` | `3e85988fada1640e` |
| `vf2-speedup-networkx__us8GDaB` | `terminal-bench/vf2-speedup-networkx` | `3e85988fada1640e` |
| `vllm-deepseek-streaming__quctgYr` | `terminal-bench/vllm-deepseek-streaming` | `4aeacc6319cce0b0` |
| `vllm-deepseek-streaming__rQhEWt9` | `terminal-bench/vllm-deepseek-streaming` | `4aeacc6319cce0b0` |
| `vllm-deepseek-streaming__t8HdFNj` | `terminal-bench/vllm-deepseek-streaming` | `4aeacc6319cce0b0` |

Excluded: none.

## Budget, arms, and comparison

- N = 3, reported at N = 1, 2, 3.
- Arms: `control`, `record-files`, `record-engine`; a fresh environment and a fresh Claude Code session for every attempt in every arm; no stop hook.
- Primary comparison: record-engine against record-files: repair rate at N = 3, paired by failure.
- Test: Mixed-effects logistic regression of repaired by N = 3 (0/1) on arm (record-engine against record-files), random intercepts for task and for failure; two-sided likelihood-ratio test of the arm effect at alpha = 0.05. If the model is singular or does not converge: exact McNemar test on the 56 failure pairs, two-sided, alpha = 0.05..

## Record, prompt, and agent

- Store schema 4, derived `3+c45ab40d3b46`: `step_flags`, `delta_flags`, `path_versions`, `trial_summary`, `step_jobs`, `delta_attribution`.
- Protocol steps sha256 `bca9ebe9add06b324c12f93c51a60ac274f9db0c26dfa5b7a3975c7b2e764c5c`; hooks file sha256 `9a84f108f6e13bb940c6bc35c8bda6f8d1d90b8031259a46eaa873a6f3fd9022`.
- Query tool: cap 200 rows, timeout 10.0 s, memory 512MB, 2 threads.
- Model `anthropic/claude-sonnet-5-5`, reasoning_effort `medium`, version `2.1.278`, agent timeout multiplier 0.5; config `configs/harbor/tb40-sonnet-v4.json`.

| Prompt prefix (arm / prior attempts) | sha256 |
|---|---|
| `control/1` | `22c378644c7d84c0eefb5cce29ed02ca82e101b5fe9c2ffd21cc5bdaf7913c1c` |
| `control/2` | `c9be85bc37120d5d650e38f74fb81fd7ee83ee247a6e92c8c9a63d96e351bb93` |
| `control/3` | `74b9c5413f67b305e7aae5092c9c8148115b6bd096726d5dbd9289c080892f56` |
| `record-files/1` | `0740f6ce140c8424eb4cde24b7fd7ad52e4775bf5c06d6df86f8216a147cf84c` |
| `record-files/2` | `42db40cd5ebf799c759028470e517d7c03c192135aafd7f523e039c0e26bbd39` |
| `record-files/3` | `f4e039cc2c384d3f2228a55d6402d43f1fda96a924c8e86b43590e55485b4025` |
| `record-engine/1` | `8d6ee59ac21e66057ecd82b550f9139cd63918b0a9254f885c01098ed9d0d2ed` |
| `record-engine/2` | `51a9d16b6564c8cebaa84028e8087b4959390ba56f2ab71912a44f1aeb476256` |
| `record-engine/3` | `9395663444c3a4fe78881f6f265091479674dfc53571ab770e8959e14ef58bdd` |

## Settings

```json
{
  "run": "tb40-exp2",
  "kind": "full",
  "corpus_job": "corpus/jobs/tb40-sonnet-v4",
  "corpus_store": "corpus/stores/tb40-sonnet-v4",
  "corpus_config": "configs/harbor/tb40-sonnet-v4.json",
  "sample": [
    {
      "trial_name": "bun-sourcemap-leak__3f2BwTC",
      "trial_id": "dc48b931-6ef1-4565-b70c-38980b756639",
      "task": "terminal-bench/bun-sourcemap-leak",
      "instruction_sha256": "48524392be65b45b3bdea9454b1d9a6a1e65879df06540e50aa5c3bbd9e32195"
    },
    {
      "trial_name": "bun-sourcemap-leak__X6KbUKo",
      "trial_id": "e068b265-57e4-4d7e-91f0-8e5efb58a4fd",
      "task": "terminal-bench/bun-sourcemap-leak",
      "instruction_sha256": "48524392be65b45b3bdea9454b1d9a6a1e65879df06540e50aa5c3bbd9e32195"
    },
    {
      "trial_name": "bun-sourcemap-leak__b5DA76u",
      "trial_id": "7f461015-2064-4cc8-b48e-dcde223a56a4",
      "task": "terminal-bench/bun-sourcemap-leak",
      "instruction_sha256": "48524392be65b45b3bdea9454b1d9a6a1e65879df06540e50aa5c3bbd9e32195"
    },
    {
      "trial_name": "cad-model__Gun964v",
      "trial_id": "177a589c-e7b8-47cb-8c09-06beddb54baa",
      "task": "terminal-bench/cad-model",
      "instruction_sha256": "354d74507e75e1fc911753c25e6aae1acc26f70c0e85c95c08aabed37ae01bc7"
    },
    {
      "trial_name": "cad-model__MEwahRs",
      "trial_id": "b7ecf80c-d3a2-4ef0-bba7-9e587744ceed",
      "task": "terminal-bench/cad-model",
      "instruction_sha256": "354d74507e75e1fc911753c25e6aae1acc26f70c0e85c95c08aabed37ae01bc7"
    },
    {
      "trial_name": "cad-model__jhjUsFh",
      "trial_id": "e4d4580a-ded7-49b9-b05d-36d4baccf788",
      "task": "terminal-bench/cad-model",
      "instruction_sha256": "354d74507e75e1fc911753c25e6aae1acc26f70c0e85c95c08aabed37ae01bc7"
    },
    {
      "trial_name": "cargo-flight-dispatch__DAgHbSz",
      "trial_id": "be1b4732-cffb-4316-946b-7991435475fc",
      "task": "terminal-bench/cargo-flight-dispatch",
      "instruction_sha256": "4c63f550eaee87595fb1b0d796f2af6ab4bb822960460a5092ba86f4fc677563"
    },
    {
      "trial_name": "cargo-flight-dispatch__FVgfyVQ",
      "trial_id": "de7f72d0-0fbb-4398-9c54-41409e80dba9",
      "task": "terminal-bench/cargo-flight-dispatch",
      "instruction_sha256": "4c63f550eaee87595fb1b0d796f2af6ab4bb822960460a5092ba86f4fc677563"
    },
    {
      "trial_name": "cargo-flight-dispatch__J8Ce4cR",
      "trial_id": "34872c38-89f1-417e-832e-19f8c7e34530",
      "task": "terminal-bench/cargo-flight-dispatch",
      "instruction_sha256": "4c63f550eaee87595fb1b0d796f2af6ab4bb822960460a5092ba86f4fc677563"
    },
    {
      "trial_name": "foodstuff-beta-activity__2iHyDYM",
      "trial_id": "a4d3acf3-8926-4964-89a3-e8737539f871",
      "task": "terminal-bench/foodstuff-beta-activity",
      "instruction_sha256": "c54a479f56a22ac3562f725f37b12a7e1640f43ae513ad796cc02ff683078cff"
    },
    {
      "trial_name": "foodstuff-beta-activity__9XLzFHA",
      "trial_id": "a4f3ce54-2479-49d8-b1d7-d680d902868b",
      "task": "terminal-bench/foodstuff-beta-activity",
      "instruction_sha256": "c54a479f56a22ac3562f725f37b12a7e1640f43ae513ad796cc02ff683078cff"
    },
    {
      "trial_name": "foodstuff-beta-activity__XZsfWUj",
      "trial_id": "9b50eaab-4ae4-4a25-add1-8164702990bf",
      "task": "terminal-bench/foodstuff-beta-activity",
      "instruction_sha256": "c54a479f56a22ac3562f725f37b12a7e1640f43ae513ad796cc02ff683078cff"
    },
    {
      "trial_name": "freecad-impeller__DddVQ9g",
      "trial_id": "1f53936b-988f-4fe2-9df0-bf74da7fd834",
      "task": "terminal-bench/freecad-impeller",
      "instruction_sha256": "2f0aebb2172e1530d5639fde3dbc1c41c8106c436eefdd799e510ab5e2c7a82f"
    },
    {
      "trial_name": "freecad-impeller__GUqwwwf",
      "trial_id": "7b3e667d-7b13-4461-94d4-a858d69366bb",
      "task": "terminal-bench/freecad-impeller",
      "instruction_sha256": "2f0aebb2172e1530d5639fde3dbc1c41c8106c436eefdd799e510ab5e2c7a82f"
    },
    {
      "trial_name": "freecad-impeller__o7pg8wY",
      "trial_id": "d9cc4120-5b80-4824-bf8a-87490596b0e3",
      "task": "terminal-bench/freecad-impeller",
      "instruction_sha256": "2f0aebb2172e1530d5639fde3dbc1c41c8106c436eefdd799e510ab5e2c7a82f"
    },
    {
      "trial_name": "freecad-platform-drawing__6tXhKU6",
      "trial_id": "0e152201-3f8b-41b7-b23a-cab500261fae",
      "task": "terminal-bench/freecad-platform-drawing",
      "instruction_sha256": "157b5310eca6c18db530692f1e1ec892bb59a6551f7829316a0daa85c2621717"
    },
    {
      "trial_name": "freecad-platform-drawing__ibz96hK",
      "trial_id": "5823db76-7dff-402d-80fc-af5f3ff94031",
      "task": "terminal-bench/freecad-platform-drawing",
      "instruction_sha256": "157b5310eca6c18db530692f1e1ec892bb59a6551f7829316a0daa85c2621717"
    },
    {
      "trial_name": "freecad-platform-drawing__yd3EsQP",
      "trial_id": "203d19cd-0a09-40b0-810c-2fe203621bd9",
      "task": "terminal-bench/freecad-platform-drawing",
      "instruction_sha256": "157b5310eca6c18db530692f1e1ec892bb59a6551f7829316a0daa85c2621717"
    },
    {
      "trial_name": "freecad-spring-clip__EmXRHs3",
      "trial_id": "db7833ac-20dd-44df-838b-223d1066284e",
      "task": "terminal-bench/freecad-spring-clip",
      "instruction_sha256": "5fbce9201a575f739f535dc5b18943e472e621da36a25b4fae9eca1d7c6d6029"
    },
    {
      "trial_name": "freecad-spring-clip__GHZZCFs",
      "trial_id": "5027019a-b382-4cd1-8f08-b0c0eb0e43d0",
      "task": "terminal-bench/freecad-spring-clip",
      "instruction_sha256": "5fbce9201a575f739f535dc5b18943e472e621da36a25b4fae9eca1d7c6d6029"
    },
    {
      "trial_name": "freecad-spring-clip__qnC44Cc",
      "trial_id": "d16b330c-b696-49e9-a289-77ad905b14e4",
      "task": "terminal-bench/freecad-spring-clip",
      "instruction_sha256": "5fbce9201a575f739f535dc5b18943e472e621da36a25b4fae9eca1d7c6d6029"
    },
    {
      "trial_name": "gsea-proteomics__jB5QsQj",
      "trial_id": "0a979de0-95d0-4977-9ef8-6072421c52a5",
      "task": "terminal-bench/gsea-proteomics",
      "instruction_sha256": "ecc0cfc999d9025673266a703a412cc0e151d1ae8a1df0f70183127cf265ccef"
    },
    {
      "trial_name": "gsea-proteomics__mqM8Mpm",
      "trial_id": "2c2f707a-6484-4df5-8c49-b86935daac5d",
      "task": "terminal-bench/gsea-proteomics",
      "instruction_sha256": "ecc0cfc999d9025673266a703a412cc0e151d1ae8a1df0f70183127cf265ccef"
    },
    {
      "trial_name": "gsea-proteomics__sqY9QmQ",
      "trial_id": "a7acac19-306d-4d59-b434-44651c7b2bdf",
      "task": "terminal-bench/gsea-proteomics",
      "instruction_sha256": "ecc0cfc999d9025673266a703a412cc0e151d1ae8a1df0f70183127cf265ccef"
    },
    {
      "trial_name": "interleaved-vigenere__N4Wfs8g",
      "trial_id": "1ee5b475-4dac-4c57-bd5c-4f61bf286dbb",
      "task": "terminal-bench/interleaved-vigenere",
      "instruction_sha256": "58af12491e556be19ad51950bca50d2dd7ed99c6e4bab10684d96bb172095eca"
    },
    {
      "trial_name": "interleaved-vigenere__wMtCHHH",
      "trial_id": "e54f4af6-a526-4a5c-a209-8f79f40bdfe8",
      "task": "terminal-bench/interleaved-vigenere",
      "instruction_sha256": "58af12491e556be19ad51950bca50d2dd7ed99c6e4bab10684d96bb172095eca"
    },
    {
      "trial_name": "layout-config-recreation2__qGZer69",
      "trial_id": "6226df27-4dfb-41b1-8541-f4a7ce7e0f4f",
      "task": "terminal-bench/layout-config-recreation2",
      "instruction_sha256": "5f3b38e66383c0547c31f6f850651cbe20401ba422b20bfaf9655ceefd801ee0"
    },
    {
      "trial_name": "layout-config-recreation2__yq2Vt6U",
      "trial_id": "bddce06f-98bc-4457-bea2-fbf77066d2fc",
      "task": "terminal-bench/layout-config-recreation2",
      "instruction_sha256": "5f3b38e66383c0547c31f6f850651cbe20401ba422b20bfaf9655ceefd801ee0"
    },
    {
      "trial_name": "layout-config-recreation__RN8QV4W",
      "trial_id": "2720e486-e1b2-4f0e-acb6-4e46391ffe27",
      "task": "terminal-bench/layout-config-recreation",
      "instruction_sha256": "cdbc86ee8a1a9514c450207182298b579580224393d6d0b657fc26153d7ce751"
    },
    {
      "trial_name": "layout-config-recreation__RV5qQjA",
      "trial_id": "5d323b96-fab3-4aa7-b387-c919e00a4734",
      "task": "terminal-bench/layout-config-recreation",
      "instruction_sha256": "cdbc86ee8a1a9514c450207182298b579580224393d6d0b657fc26153d7ce751"
    },
    {
      "trial_name": "layout-config-recreation__kb38wvM",
      "trial_id": "faa5ca51-e6f9-43cc-bf44-227617dd12e8",
      "task": "terminal-bench/layout-config-recreation",
      "instruction_sha256": "cdbc86ee8a1a9514c450207182298b579580224393d6d0b657fc26153d7ce751"
    },
    {
      "trial_name": "mvcc-lsm-compaction__PFDAk9Y",
      "trial_id": "c4cbbdba-e348-4080-a26e-f122d53e14f9",
      "task": "terminal-bench/mvcc-lsm-compaction",
      "instruction_sha256": "a168992073c0991248e620dd4c12cdf98d443df184c62d4abc3b64bfda756c9e"
    },
    {
      "trial_name": "mvcc-lsm-compaction__ccQd9vh",
      "trial_id": "49047e6d-4e43-497c-a591-eea3dcc456d4",
      "task": "terminal-bench/mvcc-lsm-compaction",
      "instruction_sha256": "a168992073c0991248e620dd4c12cdf98d443df184c62d4abc3b64bfda756c9e"
    },
    {
      "trial_name": "pretrain-shard-corruption__6egdDwc",
      "trial_id": "5fa9a80f-25f2-4403-999f-75c7c0a7aab2",
      "task": "terminal-bench/pretrain-shard-corruption",
      "instruction_sha256": "c3020127e6b5f23548e363e11549732d07b019d1476cd47fc8ed5acc41fa31ee"
    },
    {
      "trial_name": "pretrain-shard-corruption__Cgizn8v",
      "trial_id": "6c109802-0f1c-4ffe-9023-57000b77f0ee",
      "task": "terminal-bench/pretrain-shard-corruption",
      "instruction_sha256": "c3020127e6b5f23548e363e11549732d07b019d1476cd47fc8ed5acc41fa31ee"
    },
    {
      "trial_name": "pretrain-shard-corruption__R4QX4a7",
      "trial_id": "7196dd08-be98-4e0a-a5cc-863aebf83d51",
      "task": "terminal-bench/pretrain-shard-corruption",
      "instruction_sha256": "c3020127e6b5f23548e363e11549732d07b019d1476cd47fc8ed5acc41fa31ee"
    },
    {
      "trial_name": "production-planning__mXg4cWZ",
      "trial_id": "9d78df35-176c-4911-9231-92ad8f5ce6c9",
      "task": "terminal-bench/production-planning",
      "instruction_sha256": "27484dcd10cd4513faf70ad4c7e43fe1a94659bb360d35f4ac64fb1f0d60193d"
    },
    {
      "trial_name": "production-planning__un4NZWE",
      "trial_id": "a971074c-80ef-46b1-970e-eddb3942ce8b",
      "task": "terminal-bench/production-planning",
      "instruction_sha256": "27484dcd10cd4513faf70ad4c7e43fe1a94659bb360d35f4ac64fb1f0d60193d"
    },
    {
      "trial_name": "production-planning__xU3cHDY",
      "trial_id": "f2737a7d-b7cb-4585-a7ab-1707956adb07",
      "task": "terminal-bench/production-planning",
      "instruction_sha256": "27484dcd10cd4513faf70ad4c7e43fe1a94659bb360d35f4ac64fb1f0d60193d"
    },
    {
      "trial_name": "protein-autointerp-disulfide__3KtgkCt",
      "trial_id": "136611ad-7fb2-483c-aa3e-9fadf3e13016",
      "task": "terminal-bench/protein-autointerp-disulfide",
      "instruction_sha256": "110ab0a67354908e4edf34240f511e8db63a73251906871c4eb9055729d3b853"
    },
    {
      "trial_name": "protein-autointerp-disulfide__eaT4bXV",
      "trial_id": "8c4da4d5-27cf-4a76-b145-3c6611c0e969",
      "task": "terminal-bench/protein-autointerp-disulfide",
      "instruction_sha256": "110ab0a67354908e4edf34240f511e8db63a73251906871c4eb9055729d3b853"
    },
    {
      "trial_name": "protein-autointerp-disulfide__hetXe9M",
      "trial_id": "a5972fca-fbfd-4e4d-b74a-86f43e89dba0",
      "task": "terminal-bench/protein-autointerp-disulfide",
      "instruction_sha256": "110ab0a67354908e4edf34240f511e8db63a73251906871c4eb9055729d3b853"
    },
    {
      "trial_name": "roy-polymorph-cn__KVWqqpZ",
      "trial_id": "2818809a-b55c-4a5d-8ce3-6be30ba42252",
      "task": "terminal-bench/roy-polymorph-cn",
      "instruction_sha256": "1b0eaec23c28cd8a2ca44cd75290d1d5d1bbb00f1e58724feb0cdb8ddf8e5cc8"
    },
    {
      "trial_name": "roy-polymorph-cn__hrYnChi",
      "trial_id": "a70f4942-4359-4dce-af83-10a07858ccd6",
      "task": "terminal-bench/roy-polymorph-cn",
      "instruction_sha256": "1b0eaec23c28cd8a2ca44cd75290d1d5d1bbb00f1e58724feb0cdb8ddf8e5cc8"
    },
    {
      "trial_name": "roy-polymorph-cn__jizFeHm",
      "trial_id": "53fc4d45-1060-466a-af83-ebe1e3fd2de9",
      "task": "terminal-bench/roy-polymorph-cn",
      "instruction_sha256": "1b0eaec23c28cd8a2ca44cd75290d1d5d1bbb00f1e58724feb0cdb8ddf8e5cc8"
    },
    {
      "trial_name": "sglang-qwen-burst__4z9SyjA",
      "trial_id": "952eb43d-6dc0-4e02-b80f-1a8034b5f44b",
      "task": "terminal-bench/sglang-qwen-burst",
      "instruction_sha256": "36108a78dee57649577a348d8637a7d25b09b2bd2f023581707e6463aa00d2cc"
    },
    {
      "trial_name": "sglang-qwen-burst__Y6ovEBk",
      "trial_id": "a2cc2171-4e32-4a9c-9200-8426a71b04d2",
      "task": "terminal-bench/sglang-qwen-burst",
      "instruction_sha256": "36108a78dee57649577a348d8637a7d25b09b2bd2f023581707e6463aa00d2cc"
    },
    {
      "trial_name": "sglang-qwen-burst__exuuLAH",
      "trial_id": "70c6c9ea-1b88-465b-a3e8-708d4bc7b913",
      "task": "terminal-bench/sglang-qwen-burst",
      "instruction_sha256": "36108a78dee57649577a348d8637a7d25b09b2bd2f023581707e6463aa00d2cc"
    },
    {
      "trial_name": "sound-change-cascade__WGwuZ5g",
      "trial_id": "8de4ba22-512f-4608-83af-fa4407c6ec1c",
      "task": "terminal-bench/sound-change-cascade",
      "instruction_sha256": "255f6563177da56dddd0d6958b8747e30d584bc46d1dcb746f30b74c09c4c68e"
    },
    {
      "trial_name": "vba-userform-port__PFng8aw",
      "trial_id": "0ed2fce5-ede9-4137-b6eb-e420b96a253a",
      "task": "terminal-bench/vba-userform-port",
      "instruction_sha256": "c68538f5ede995de4d585b601ab945f25f023fdc9406b7b8860b76094f09f571"
    },
    {
      "trial_name": "vf2-speedup-networkx__UVeaJc9",
      "trial_id": "23a56881-52c8-494b-a5ee-eb213bc61ec9",
      "task": "terminal-bench/vf2-speedup-networkx",
      "instruction_sha256": "3e85988fada1640edba0f8b519e24977bf2b7bf94059f9e127dd56086d3dff84"
    },
    {
      "trial_name": "vf2-speedup-networkx__bDWTHvP",
      "trial_id": "a1a0ed0a-999a-4e23-aba7-c689727bef1e",
      "task": "terminal-bench/vf2-speedup-networkx",
      "instruction_sha256": "3e85988fada1640edba0f8b519e24977bf2b7bf94059f9e127dd56086d3dff84"
    },
    {
      "trial_name": "vf2-speedup-networkx__us8GDaB",
      "trial_id": "21f335f3-9d4e-4e6a-8163-75692f83002c",
      "task": "terminal-bench/vf2-speedup-networkx",
      "instruction_sha256": "3e85988fada1640edba0f8b519e24977bf2b7bf94059f9e127dd56086d3dff84"
    },
    {
      "trial_name": "vllm-deepseek-streaming__quctgYr",
      "trial_id": "80585085-0c24-4485-9214-24b271e19803",
      "task": "terminal-bench/vllm-deepseek-streaming",
      "instruction_sha256": "4aeacc6319cce0b0c5d7a4cd2864abf316dc9a5baae88956b8dfe176ed773793"
    },
    {
      "trial_name": "vllm-deepseek-streaming__rQhEWt9",
      "trial_id": "0a0549d1-54b0-41c5-ba96-413083c4b487",
      "task": "terminal-bench/vllm-deepseek-streaming",
      "instruction_sha256": "4aeacc6319cce0b0c5d7a4cd2864abf316dc9a5baae88956b8dfe176ed773793"
    },
    {
      "trial_name": "vllm-deepseek-streaming__t8HdFNj",
      "trial_id": "0069b367-1a65-48b5-ba73-f8d69c1826d3",
      "task": "terminal-bench/vllm-deepseek-streaming",
      "instruction_sha256": "4aeacc6319cce0b0c5d7a4cd2864abf316dc9a5baae88956b8dfe176ed773793"
    }
  ],
  "exclusions": [],
  "budget": 3,
  "report_at": [
    1,
    2,
    3
  ],
  "arms": [
    "control",
    "record-files",
    "record-engine"
  ],
  "primary": "record-engine against record-files: repair rate at N = 3, paired by failure",
  "test": "Mixed-effects logistic regression of repaired by N = 3 (0/1) on arm (record-engine against record-files), random intercepts for task and for failure; two-sided likelihood-ratio test of the arm effect at alpha = 0.05. If the model is singular or does not converge: exact McNemar test on the 56 failure pairs, two-sided, alpha = 0.05.",
  "schema_version": 4,
  "derived": "3+c45ab40d3b46",
  "derived_views": [
    "step_flags",
    "delta_flags",
    "path_versions",
    "trial_summary",
    "step_jobs",
    "delta_attribution"
  ],
  "protocol_sha256": "bca9ebe9add06b324c12f93c51a60ac274f9db0c26dfa5b7a3975c7b2e764c5c",
  "prefix_sha256": {
    "control/1": "22c378644c7d84c0eefb5cce29ed02ca82e101b5fe9c2ffd21cc5bdaf7913c1c",
    "control/2": "c9be85bc37120d5d650e38f74fb81fd7ee83ee247a6e92c8c9a63d96e351bb93",
    "control/3": "74b9c5413f67b305e7aae5092c9c8148115b6bd096726d5dbd9289c080892f56",
    "record-files/1": "0740f6ce140c8424eb4cde24b7fd7ad52e4775bf5c06d6df86f8216a147cf84c",
    "record-files/2": "42db40cd5ebf799c759028470e517d7c03c192135aafd7f523e039c0e26bbd39",
    "record-files/3": "f4e039cc2c384d3f2228a55d6402d43f1fda96a924c8e86b43590e55485b4025",
    "record-engine/1": "8d6ee59ac21e66057ecd82b550f9139cd63918b0a9254f885c01098ed9d0d2ed",
    "record-engine/2": "51a9d16b6564c8cebaa84028e8087b4959390ba56f2ab71912a44f1aeb476256",
    "record-engine/3": "9395663444c3a4fe78881f6f265091479674dfc53571ab770e8959e14ef58bdd"
  },
  "hooks_sha256": "9a84f108f6e13bb940c6bc35c8bda6f8d1d90b8031259a46eaa873a6f3fd9022",
  "stop_hook": null,
  "query": {
    "arm": "record-engine",
    "ingests": true,
    "mounts_bundle": false,
    "serves_queries": true,
    "query_cap": 200,
    "query_timeout_s": 10.0,
    "query_memory_limit": "512MB",
    "query_threads": 2
  },
  "model": "anthropic/claude-sonnet-5-5",
  "agent_kwargs": {
    "reasoning_effort": "medium",
    "version": "2.1.278"
  },
  "agent_timeout_multiplier": 0.5,
  "repo_sha": "198ab1d537c395ed5562ded2a706cb6d0bc8a60b"
}
```
