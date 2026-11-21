# FAST Team - Blast Radius Test Selector

Entry for the RWX Blast Radius track at Builders Buildathon 2026.

## Approach

Given patches to sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, we select the minimal set of tests that could fail.

### Key Techniques

1. **Line-level coverage mapping**: Pre-built maps from running tests with coverage tracking. Each source line maps to the tests that execute it.

2. **AST-based semantic diff**: Patches that only change comments or whitespace (same AST) select no tests.

3. **Fixture tracking**: SQL fixture files map to the tests that load them.

4. **Import-time analysis**: Changes at module level (class definitions, constants) are tracked through import dependency graphs.

### Maps

Built offline from the pinned commit:
- `coverage.json.gz` - Line-level test coverage
- `fixtures.json` - Fixture file to test mapping  
- `classes.json.gz` - Class instantiation tracking
- `import_lines.json` - Import-time code locations
- `import_groups.json.gz` - Import dependency groups

### Worker Scheduling

Tests are packed into 1-8 workers minimizing `max_worker_time + 2*num_workers`.

## Local Testing

```bash
docker build -t fast-blast .
docker run --rm -v $(pwd)/sample:/data fast-blast > output.jsonl
python3 score.py sample sample/answers.json output.jsonl
```

## Results

- Sample score: **118.4s simulated, 0 misses**
- Build time: ~6 seconds (maps pre-committed)
