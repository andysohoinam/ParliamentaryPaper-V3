# Parliamentary Paper Crawler

Upload all files while preserving `.github/workflows/run_crawl.yml`, then run **Actions > Parliament Crawler > Run workflow**.

Optional repository secret: `CRAWLER_CONTACT`.

The workflow uploads `crawler_output` containing the two CSV files, downloaded PDFs, status CSV, summary JSON, and logs. A zero-data run fails clearly after uploading diagnostics.
