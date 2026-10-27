# Sri Omkar D — Wide Resume (base for dynamic tailoring)

Source: Sri_Omkar-2.pdf (uploaded 2026-10-27). Faithful extraction: every
point from the wide resume, in the same order. Nothing added, nothing removed.
If the PDF is revised, re-extract this file so the pipelines stay in sync.
The wide resume itself carries no AI/MCP section; the AI/MCP project points
used by the outreach email live in the email prompt's AI/MCP bank, not here.

sri.omkar.d@gmail.com | +1 951-545-2146 | sriomkar.com | linkedin.com/in/sri-omkar-58r4r4r8

## Professional Summary

- Data Engineer with 7+ years of experience building and supporting batch and near-real-time data pipelines across AWS and Azure, using Python, SQL, PySpark, Apache Spark, Databricks, AWS Glue, Airflow, S3, ADLS Gen2, Redshift, Snowflake, Teradata, and SQL Server.
- Experienced in healthcare and enterprise legacy migrations, converting complex IBM DataStage and SSIS workflows into HIPAA/HITRUST-compliant AWS pipelines (S3, Glue, Lambda, PySpark, Redshift) with strict parallel parity validation.
- Hands-on experience supporting equipment rental and fleet operations data pipelines, working with data from RentalMan, AS400, Oracle OLTP, Teradata, and internal equipment monitoring applications for rental operations, fleet utilization, billing, and business reporting.
- Experienced with Databricks, PySpark, Spark SQL, AWS S3, AWS Kinesis, and Databricks Streaming for processing structured, semi-structured, and near-real-time operational data.
- Built and supported Azure-based data engineering pipelines for retail supply chain and client ERP datasets using ADLS Gen2, Azure Databricks, PySpark, FastAPI, SQLAlchemy, Snowflake, Exasol, Terraform, Docker, Azure Key Vault, Azure Monitor, and GitHub Actions.
- Worked on AWS data lake and banking compliance pipelines involving legacy Ab Initio workflows, PySpark transformations, AWS Glue Data Catalog, Airflow orchestration, Redshift reporting, Git, and Jenkins.
- Strong background in traditional ETL and analytics workflows using SQL Server, Sqoop, AWS EMR, HiveQL, AWS Glue, PySpark, Amazon Redshift, Athena, Tableau, and Power BI for sales, subscription, revenue, and customer usage reporting.
- Skilled in SQL development, data validation, source-to-target reconciliation, pipeline troubleshooting, job monitoring, dimensional modeling, query tuning, partitioning, joins, aggregations, window functions, and performance optimization across cloud and legacy data platforms.
- Comfortable working with business users, analysts, data teams, and project stakeholders to gather requirements, support production data loads, document workflows, and deliver reporting-ready datasets for Qlik Sense, Tableau, and Power BI.

## Technical Skills

- Cloud Platforms: AWS, Microsoft Azure
- AWS Services: S3, EMR, Athena, Kinesis, AWS Glue, Glue Data Catalog, IAM, CloudWatch, Lambda
- Azure Services: ADLS Gen2, Azure Databricks, Azure Key Vault, Azure Monitor, Log Analytics, Azure RBAC
- Big Data & Processing: Apache Spark, PySpark, Spark SQL, Databricks, Databricks Streaming, HiveQL, Sqoop, Delta Lake, Medallion Architecture (Bronze/Silver/Gold)
- Programming & Scripting: Python, SQL
- Data Warehousing & Databases: Amazon Redshift, Snowflake, Exasol, Teradata, Microsoft SQL Server, Oracle OLTP, AS400
- Orchestration & DevOps: Apache Airflow, SQL Server Agent, Docker, Terraform, Git, GitHub Actions, Jenkins, CI/CD, dbt, SSIS, IBM DataStage, Ab Initio
- Data Modeling & Architecture: Star Schema, Snowflake Schema, Fact/Dimension Tables, SCD Type 2, Materialized Views, CDC
- Python Libraries & Frameworks: FastAPI, SQLAlchemy, Pandas, NumPy, SciPy, Matplotlib, Scikit-learn
- BI & Reporting: Qlik Sense, Tableau, Power BI
- Tools & Collaboration: Jira, Confluence, Agile/Scrum
- Spark Optimization: Spark UI, AQE, Broadcast Joins, Data Salting, Partition Tuning, Spark Catalyst
- File Formats: Parquet, JSON, XML, CSV
- Data Quality & Governance: HIPAA, HITRUST, PHI Data Masking, RBAC, Source-to-Target Reconciliation, Data Validation, Duplicate Checks, Row Count & Variance Checks

## Professional Experience

### Optum, Florida — Data Engineer, Oct 2025 - Present

- Led the end-to-end cloud migration of 30+ legacy ETL workflows to AWS across two execution waves — converting 12 SSIS packages (provider network feeds) and 22 IBM DataStage jobs (claims and pharmacy billing) into modular AWS Glue, PySpark, AWS Lambda, S3, and Amazon Redshift pipelines.
- Cut a critical nightly batch runtime by ~50% — bringing completion from 7:30 AM (breaching the 6:00 AM SLA) down to 3:00 AM — by restructuring S3 partitioning around date and provider ID and optimizing Amazon Redshift COPY ingestion commands.
- Re-engineered a failing legacy DataStage XML parsing stage from scratch using PySpark, flattening deeply nested patient arrays and enforcing strict schema validation before landing data in the S3 raw zone.
- Enforced HIPAA and HITRUST compliance across all healthcare pipelines by implementing PHI data masking, column-level minimization, Role-Based Access Control (RBAC), and end-to-end data lineage from raw ingestion to analytical reporting.
- Executed strict cutover governance through parallel dual-run validations, automated row-count parity checks, column-by-column regression testing, and UAT sign-offs before decommissioning legacy DataStage and SSIS jobs.
- Profiled, deduplicated, and standardized historical healthcare datasets prior to migration, establishing data archival rules and automated schema-drift checks to eliminate single points of failure.
- Orchestrated and monitored production batch schedules in Apache Airflow, configuring complex DAG dependencies, automated retry logic, SLA alerting, and root-cause remediation for overnight production loads.
- Authored comprehensive source-to-target mapping documentation, migration runbooks, and operational troubleshooting guides in Confluence to transition migrated pipelines smoothly to production support teams.

### Akkodis (Herc Rentals Inc.), Florida — Data Engineer, Nov 2024 - Sep 2025

- Developed Python-based ingestion workflows to route high-volume operational data from RentalMan, AS400, Oracle OLTP, and Teradata-based systems into an AWS S3 data lake for rental, inventory, billing, and branch operations reporting.
- Built near-real-time equipment monitoring pipelines using AWS Kinesis and Databricks Streaming to process semi-structured JSON/XML telemetry payloads, including GPS location, engine metrics, fuel usage, and fleet activity for maintenance and utilization reporting.
- Created and maintained PySpark and Spark SQL pipelines in Databricks (Delta Lake) across a Medallion Architecture (Bronze, Silver, Gold) to clean, normalize, and join Change Data Capture (CDC) datasets across rental contracts, fleet inventory, billing, and equipment usage.
- Reduced daily batch runtimes by approximately 25–30% by tuning PySpark partition strategies, optimizing Spark SQL joins, and replacing unnecessary full-refresh cycles with scheduled incremental load patterns.
- Decreased pipeline rerun effort by approximately 30–40% by breaking long Databricks transformation chains into modular, restartable stages with intermediate curated outputs for rental, billing, and fleet reporting loads.
- Added data validation and source-to-target reconciliation checks across pipeline stages, including record counts, duplicate checks, null handling, and source-to-target comparisons to improve data reliability before business reporting.
- Built SQL-based curated views and dimensional reporting layers using Teradata and Databricks datasets, implementing Fact/Dimension modeling, SCD Type 2 historical tracking, and materialized views to support Qlik Sense dashboards for fleet utilization and financial reporting.
- Orchestrated scheduled workflows using Apache Airflow and SQL Server Agent, including dependency handling, retry logic, monitoring, and troubleshooting for failed loads.

### Quess Corp (Blue Yonder India Pvt Ltd.), India — Data Engineer, June 2022 - July 2023

- Reduced batch transformation runtimes by approximately 25–30% by consolidating iterative column casting and enrichment logic into unified PySpark DataFrame operations, reducing Spark Catalyst planning overhead.
- Optimized execution plans for large-scale supply chain datasets, including inventory, POS, orders, and product movement, by eliminating unnecessary repartitioning, tuning partition boundaries, and optimizing Spark SQL joins.
- Developed distributed Azure Databricks pipelines to clean, deduplicate, normalize, and join high-volume retail supply chain data for downstream retail optimization and analytics use cases.
- Built programmatic data ingestion frameworks using Python, FastAPI, and SQLAlchemy to validate, standardize, and route client ERP datasets from APIs, flat files, databases, and source systems into Azure Data Lake Storage Gen2 (ADLS Gen2).
- Maintained secure, repeatable Azure data environments and automated CI/CD deployment pipelines using Terraform, Docker, GitHub Actions, Azure Key Vault, and Azure RBAC, supporting controlled access and reduced compute overhead.
- Implemented pipeline monitoring and logging using Azure Monitor and Log Analytics to track job health, runtime performance, failed loads, and operational issues across ingestion and transformation workflows.
- Supported SQL query tuning, execution profiling, and modular dbt / dimensional data model transformations within Snowflake and Exasol, improving reporting performance for supply chain analytics workloads.
- Managed Apache Airflow DAG dependencies and scheduled batch workflows, including retries, dependency handling, SLA monitoring, and alerting for Databricks processing jobs.

### Trigent Software Pvt Ltd. (Accenture Solutions Pvt Ltd.), India — Data Engineer, Jan 2020 - June 2022

- Reduced runtime by approximately 35–40% on selected high-volume financial pipelines by using Spark UI to diagnose data skew and applying data salting, broadcast joins, Adaptive Query Execution (AQE), and caching strategies.
- Optimized incremental load performance by replacing costly full-table merge patterns with partition-aware anti-join and append logic for insert-only datasets.
- Built and maintained distributed PySpark and Spark SQL transformation jobs to perform filtering, complex joins, aggregations, deduplication, enrichment, and source-to-target validation for regulatory compliance reporting.
- Supported modernization of legacy Ab Initio workflows by developing Python-based ingestion services and processing components, moving high-volume banking datasets into an AWS S3 data lake organized across raw, cleansed, and curated layers.
- Registered and structured data lake assets in AWS Glue Data Catalog, enabling metadata discovery across Spark, SQL, and Amazon Redshift query workflows.
- Modeled and loaded audit-ready financial datasets into Amazon Redshift, creating optimized SQL reporting tables and materialized views to support Financial Crime and Business Intelligence teams.
- Performed source-to-target data quality validation, reconciliation, duplicate checks, and variance checks across large financial datasets to support compliance reporting accuracy and SLA expectations.
- Managed, monitored, and automated batch DAGs in Apache Airflow, implementing retry logic, log tracking, dependency management, and failure alerting for compliance loads.
- Maintained CI/CD deployment workflows using Git and Jenkins, supporting safe, version-controlled code migration across DEV, QA, and PROD environments.

### Experis IT (Thomson Reuters), India — ETL Developer, June 2017 - Sep 2019

- Built and supported large-scale data extraction workflows from legacy SQL Server databases into an AWS S3 data lake using Apache Sqoop on AWS EMR.
- Authored HiveQL scripts to process and transform large flat files and relational datasets within the Hadoop ecosystem for downstream analytical consumption.
- Reduced selected batch write delays by approximately 20–25% by tuning Parquet output write patterns, partition strategies, and S3 load behavior for AWS EMR and Spark workloads.
- Developed AWS Glue and PySpark jobs to clean, aggregate, and summarize sales, subscription, and revenue BI metrics before loading curated datasets into executive dashboards.
- Used Amazon Redshift and AWS Athena to power Tableau and Power BI dashboards for sales, subscription, and revenue reporting.
- Wrote complex SQL queries using window functions, subqueries, aggregations, and multi-table joins to support ad-hoc analysis, reporting, data validation, and query tuning for business stakeholders.
- Supported end-to-end batch ingestion workflows to extract sales, subscription, customer usage, and revenue data from on-premise applications into a centralized analytics environment.
- Implemented daily data validation checks, including missing record detection, duplicate checks, source-to-target row count validation, and revenue variance reviews to improve dashboard accuracy and reporting timelines for sales and customer usage reporting.
- Monitored pipeline execution, diagnosed failed data loads, and troubleshot ETL bottlenecks to support scheduled loads and dashboard reliability.

## Education

- Masters in Information Technology Management, 2024 — California Baptist University, Riverside, CA
- B. Tech in Computer Science and Engineering, 2017 — JNTU-K University, India
