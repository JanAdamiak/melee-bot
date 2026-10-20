# Contributing

Thanks for your interest in melee-bot! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or asking for a feature

Open an issue. For bugs, include what you ran, what you expected, and what happened instead. A melee.gg tournament ID helps if the problem is tied to a specific event. Please don't paste your bot token or `.env`.

## Making a change

1. Fork the repo and create a branch from `main`.
2. Set up the project (see [Local setup](README.md#local-setup)):

   ```bash
   uv sync --group dev
   ```

3. Make your change, and add or update tests in `tests/` for anything that changes behaviour.
4. Check that lint and tests pass. CI runs the same two commands:

   ```bash
   uv run ruff check .
   uv run pytest
   ```

5. Open a pull request describing what changed and why.

Tests never touch the network: HTTP calls are mocked, and the captured melee.gg data lives in `tests/fixtures/`. To see the bot working end to end without a live event, use the fake melee.gg in [`demo_fake_melee/`](demo_fake_melee/README.md).

## Being kind to melee.gg

The bot reads public melee.gg pages and keeps to the site's `Crawl-Delay` of 5 seconds between requests. Please keep it that way: don't remove the delay or add bulk scraping. If you capture new fixtures, replace players' usernames and display names with invented ones before committing them.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
