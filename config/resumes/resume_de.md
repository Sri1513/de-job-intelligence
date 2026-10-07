# Candidate Profile & Matching Criteria

## Target Preferences (Hard Filters)
* **Target Titles:** Data Engineer, Senior Data Engineer, ETL Developer
* **Preferred Work Setup:** Remote, Hybrid, or On-site (United States)
* **Target Compensation:** Market rate for 7+ years of experience
* **Preferred Tech Stack:** IBM DataStage, Microsoft SSIS, AWS (S3, Glue, Lambda, Redshift), PySpark, SQL, Apache Airflow
* **Dealbreaker Roles:** Roles with no data engineering or ETL component (pure frontend, pure management).

---

## Core Technical Skills
* **ETL Tools:** IBM DataStage (Parallel / Server / Sequence Jobs), Microsoft SSIS (Control Flow, Data Flow), Ab Initio, AWS Glue, dbt Core
* **Cloud Platforms:** AWS (S3, Glue, Glue Data Catalog, Redshift, Lambda, Athena, EMR, Kinesis, IAM, CloudWatch), Azure (ADLS Gen2, Azure Databricks, Key Vault, Monitor, RBAC)
* **Programming:** SQL (T-SQL, PL/SQL, Spark SQL, HiveQL), Python, PySpark
* **Databases & Warehousing:** SQL Server, Oracle, Teradata, DB2, Snowflake, Redshift, Exasol. Dimensional modeling (star/snowflake, SCD Type 2), MPP architectures
* **Scheduling & Orchestration:** SQL Server Agent, Apache Airflow, Control-M concepts, cron
* **Data Quality & Governance:** Source-to-target reconciliation, parity checks, profiling, cleansing, de-duplication, data lineage, HIPAA, HITRUST, data masking, RBAC
* **DevOps:** Git, GitHub Actions, Jenkins, Terraform, Docker, CI/CD
* **BI & Reporting:** Tableau, Power BI, Qlik Sense

---

## Professional Experience

### Data Engineer | Nortek Consulting INC (via Akkodis), Client: Optum
*Florida | Nov 2024 – Present*
* Take apart legacy DataStage and SSIS jobs. Map the dependencies, the business logic buried in them, source to target. Figure out what it takes to move them to cloud.
* Rebuild those jobs as AWS pipelines (S3, Glue, Lambda, Redshift) with proper logging, scheduling, and error handling. Shut the old jobs down once parity checks pass.
* Migrated 30+ legacy jobs to AWS across two waves. 12 SSIS packages (provider network feeds) first, then 22 DataStage jobs (claims and pharmacy billing). Every one through parallel validation before the old job was shut down.
* Rebuilt a failed DataStage XML parsing stage from scratch in PySpark during migration. Nested patient arrays broke the legacy parser. Enforced a strict schema before writing to the S3 raw zone.
* Build HIPAA/HITRUST controls into the pipelines. PHI masking, RBAC, only the fields we need, full lineage from raw to reporting so audits are painless.
* Validate everything. Row counts, parity checks, schema checks, regression tests, UAT. Run the new pipeline side by side with the old one and compare column by column before cutover.
* Cut the worst nightly load from blowing past the 6 AM SLA (often 7:30) to done by 3 AM. Partitioned by date and provider ID, tuned the Redshift COPY. Roughly half the runtime.
* Production support for DataStage/SSIS batches. Watch the overnight runs, fix breaks, find root cause when something fails, catch data issues before they hit downstream reports. Then fix it for good so it doesn't repeat.
* Work with architects, DBAs, and business teams on migration plans and cutovers. Write the mapping docs and runbooks.
* Trial migrations first. Dual parallel runs, row-by-row parity, then cut over with hypercare until things are stable.
* Profile and clean data before migration. Dedupe, handle nulls, decide what old data gets archived instead of moved.
* Set standards for data checks across the pipeline. Kill single points of failure before they kill a run.
* Own the batch schedules in Airflow. Set up the job chains, dependencies, retries. If a job misses its window, I'm the one who gets the alert.
* Hand off migrated pipelines to the support team. Walk them through the runbooks, the known quirks, what to watch for at 2 AM.

### Data Engineer | Quess Corp (Blue Yonder India Pvt Ltd.)
*India | June 2022 – July 2023*
* Owned ETL for retail supply chain data. Inventory, POS, orders, product movement. Sources were ERP systems, flat files, APIs. Target was the Azure data lake and Snowflake.
* Every transformation started with a source-to-target mapping. What comes in, what goes out, the business rules in between. Nothing got built until the mapping was signed off.
* Wrote the transformation logic in PySpark and SQL. Merged scattered enrichment steps into single unified runs. Cut batch runtimes by 25–30%.
* Tested transformations before they touched production. Ran them on sample data, checked outputs row by row, then pointed them at the real tables.
* Built validation into every stage. Duplicate checks, null handling, source-to-target row comparisons. A load that failed a check stopped there instead of corrupting downstream.
* Tuned the slow parts. Profiled queries on Snowflake and Exasol, fixed joins, redrew partitions on the big tables.
* Ran the batches in Airflow and owned what happened after. DAGs, retries, SLA alerts. When something broke overnight, I found why and fixed it. Wrote the docs so the next person doesn't have to guess.

### Data Engineer | Trigent Software Pvt Ltd. (Accenture Solutions Pvt Ltd.)
*India | Jan 2020 – June 2022*
* Migrated legacy Ab Initio workflows to AWS. Mapped every old job first, then rebuilt from the map.
* Landed data in S3 across raw, cleansed, and curated zones. No mixing.
* Rebuilt transforms in PySpark on Glue. Registered every table in the Glue Data Catalog.
* Modeled Redshift dims and facts. Set dist and sort keys for how reports actually query.
* Ran old and new side by side before cutover. Row counts, column parity. Switched over only when the numbers tied.
* Cut runtime 35–40% on financial pipelines. Diagnosed skew in Spark UI, applied salting, broadcast joins, AQE.
* Baked source-to-target validation into every job. Regulatory reporting, zero room for wrong numbers.
* Airflow for scheduling, CI/CD with Git and Jenkins across DEV, QA, PROD. Handed over with runbooks.
* Automated the pipeline plumbing in Python. File checks, rerun scripts, alerts between jobs. The unglamorous code that keeps batches moving.
* Used Athena to query raw S3 data directly during migration. Fastest way to compare legacy output against new tables without loading anything.
* Replaced 800+ line Redshift stored procedures with dbt Core models, run through Airflow BashOperator on EC2. The old procedural SQL had no tests and no lineage — a failure halfway through meant manual rollbacks and cleaning up intermediate tables by hand.
* Built dbt models in layers: stg_ views over raw Redshift tables and S3 feeds, int_ for customer risk aggregation and transaction joins, dim_/fct_ marts for KYC risk scoring and daily loan ledger reconciliations. Incremental models on composite keys with a 3-day lookback, so late-arriving loan transactions landed without rescanning years of history.
* Put dbt tests on everything that mattered: unique, not_null, relationships on customer and transaction IDs, plus custom SQL tests proving daily loan ledger debits and credits balanced to zero before compliance saw the data. Cut incremental runtimes from over an hour to under 45 minutes. Caught duplicates and null-key leaks at the test stage instead of in audit.

### ETL Developer | Experis IT (Thomson Reuters)
*India | June 2017 – Sep 2019*
* Built SSIS packages to extract from legacy SQL Server databases into an S3 data lake. Control flow for orchestration, data flow for transforms. Sales, subscription, and usage data.
* Where SSIS wasn't the right fit, built the transforms in Glue, PySpark, and HiveQL.
* Wrote heavy SQL daily. Window functions, subqueries, multi-table joins. For validation, ad-hoc analysis, tuning.
* Ran data checks every morning. Missing records, duplicates, source-to-target counts, revenue variance. Before the reports went out.
* Sped up slow batches 20–25%. Parquet outputs, fixed partitioning.
* Scheduled and watched everything on SQL Server Agent. Diagnosed failed loads, protected SLAs.
* Wrote T-SQL procedures, functions, and views behind the ETL and reports. Automated routine ops with UNIX scripts.

---

## Education & Certifications
* **Master's in Information Technology Management** – California Baptist University, Riverside, CA (2024)
* **B.Tech in Computer Science and Engineering** – JNTU-K University, India (2017)
