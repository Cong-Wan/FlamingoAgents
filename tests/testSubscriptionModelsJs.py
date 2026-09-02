'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Runs Node assertions for safe candidate merging, non-overwrite/idempotency, ambiguity, discovery guards, account-epoch flight isolation, and inverse asynchronous open commit rejection.
'''

from __future__ import annotations

import subprocess
from pathlib import Path


def testSubscriptionCandidateMergeAndRaceHelpers() -> None:
    scriptPath = Path('webApp/frontend/js/subscriptionModels.js').resolve()
    nodeScript = r'''
const assert = require('assert');
require(process.argv[1]);
const helpers = global.subscriptionModels;

function model(id, contextWindow) {
  return {
    id: id, name: id, input: ['text'], contextWindow: contextWindow,
    maxTokens: 100, reasoning: true,
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
  };
}
function discovery(models) {
  return {
    provider: 'xai', source: 'live-catalog-match', autoApplicable: true,
    providerTemplate: {
      suggestedId: 'xaiSubscription', baseUrl: 'https://api.x.ai/v1',
      api: 'openai-responses', auth: 'oauth', headers: {}, models: models
    },
    report: {warnings: [], skippedModels: []}
  };
}

const existingModel = Object.assign(model('grok-4.6', 777), {custom: {keep: true}});
const config = {providers: {
  existingXai: {
    baseUrl: 'https://API.X.AI/v1/', api: 'openai-responses', auth: 'oauth',
    headers: {'X-Custom': 'keep'}, extension: {nested: 1}, models: [existingModel]
  },
  legacy: {baseUrl: 'https://relay.example/v1', api: 'openai-completions', auth: 'api-key', models: [model('legacy', 1000)]}
}};
const before = JSON.stringify(config);
const candidate = discovery([model('grok-4.6', 500000), model('grok-4.5', 500000)]);
const merged = helpers.mergeDiscovery(config, candidate, null);
assert.strictEqual(merged.ok, true);
assert.strictEqual(merged.providerId, 'existingXai');
assert.deepStrictEqual(merged.addedModelIds, ['grok-4.5']);
assert.deepStrictEqual(merged.keptModelIds, ['grok-4.6']);
assert.strictEqual(JSON.stringify(config), before);
assert.strictEqual(merged.config.providers.existingXai.models[0].contextWindow, 777);
assert.deepStrictEqual(merged.config.providers.existingXai.models[0].custom, {keep: true});
assert.deepStrictEqual(merged.config.providers.existingXai.headers, {'X-Custom': 'keep'});
assert.deepStrictEqual(merged.config.providers.existingXai.extension, {nested: 1});
assert.deepStrictEqual(merged.config.providers.existingXai.models.map(x => x.id), ['grok-4.6', 'grok-4.5']);

candidate.providerTemplate.models[1].name = 'mutated';
assert.notStrictEqual(merged.config.providers.existingXai.models[1].name, 'mutated');
const repeated = helpers.mergeDiscovery(merged.config, discovery([model('grok-4.6', 1), model('grok-4.5', 1)]), null);
assert.deepStrictEqual(repeated.addedModelIds, []);
assert.deepStrictEqual(repeated.config, merged.config);

const collisionConfig = {providers: {
  xaiSubscription: {baseUrl: 'https://relay.example/v1', api: 'openai-completions', auth: 'api-key', models: [model('other', 100)]}
}};
const collision = helpers.mergeDiscovery(collisionConfig, discovery([model('grok-4.6', 1)]), null);
assert.strictEqual(collision.providerId, 'xaiSubscription2');
assert.strictEqual(collision.createdProvider, true);
assert.ok(collision.config.providers.xaiSubscription);
assert.ok(collision.config.providers.xaiSubscription2);

const ambiguousConfig = {providers: {
  first: {baseUrl: 'https://api.x.ai/v1', api: 'openai-responses', auth: 'oauth', models: []},
  second: {baseUrl: 'https://api.x.ai/v1/', api: 'openai-responses', auth: 'oauth', models: []}
}};
const ambiguousBefore = JSON.stringify(ambiguousConfig);
const ambiguous = helpers.mergeDiscovery(ambiguousConfig, discovery([model('grok-4.6', 1)]), null);
assert.strictEqual(ambiguous.ok, false);
assert.strictEqual(ambiguous.code, 'ambiguous_provider');
assert.strictEqual(JSON.stringify(ambiguousConfig), ambiguousBefore);
const selected = helpers.mergeDiscovery(ambiguousConfig, discovery([model('grok-4.6', 1)]), 'second');
assert.strictEqual(selected.providerId, 'second');
assert.strictEqual(selected.config.providers.first.models.length, 0);
assert.strictEqual(selected.config.providers.second.models.length, 1);

const dangerous = discovery([model('grok-4.6', 1)]);
dangerous.providerTemplate.suggestedId = '__proto__';
assert.throws(() => helpers.mergeDiscovery({providers: {}}, dangerous, null), /providerId/);
const polluted = JSON.parse('{"providers":{"safe":{"baseUrl":"https://api.x.ai/v1","api":"openai-responses","auth":"oauth","models":[],"extension":{"__proto__":{"polluted":true}}}}}');
assert.throws(() => helpers.mergeDiscovery(polluted, discovery([model('grok-4.6', 1)]), null), /危险字段/);
assert.strictEqual({}.polluted, undefined);

assert.strictEqual(helpers.canApplyResult(4, 4, 2, 2, 7, 7), true);
assert.strictEqual(helpers.canApplyResult(4, 5, 2, 2, 7, 7), false);
assert.strictEqual(helpers.canApplyResult(4, 4, 2, 3, 7, 7), false);
assert.strictEqual(helpers.canApplyResult(4, 4, 2, 2, 7, 8), false);
let currentOpenRevision = 1;
const oldOpenRevision = currentOpenRevision;
currentOpenRevision += 1;
const newOpenRevision = currentOpenRevision;
assert.strictEqual(helpers.canCommitOpen(oldOpenRevision, currentOpenRevision), false);
assert.strictEqual(helpers.canCommitOpen(newOpenRevision, currentOpenRevision), true);
const oldFlight = helpers.flightKey('xai', 1);
const newFlight = helpers.flightKey('xai', 3);
assert.notStrictEqual(oldFlight, newFlight);
const flights = {};
flights[oldFlight] = 'old-request';
flights[newFlight] = 'new-request';
delete flights[oldFlight];
assert.strictEqual(flights[newFlight], 'new-request');
console.log('subscriptionModels.js: ok');
'''
    result = subprocess.run(
        ['node', '-e', nodeScript, str(scriptPath)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'subscriptionModels.js: ok' in result.stdout
