# Related work

dbq combines ideas that are each well established. This section credits them and states what
is specific to dbq. It is based on a short literature check (October 2026), not an exhaustive
survey.

## Semantic layers

The catalog and plan compiler follow the *semantic layer* pattern of business-intelligence
tools. Measures, dimensions and join paths are declared once, and a compiler turns a request
such as "measure by dimension, filtered" into SQL with the right joins and aggregation.
Examples are dbt's Semantic Layer (MetricFlow), which compiles YAML-defined metrics into
SQL through a query plan, and Looker's LookML. Snowflake's Cortex Analyst combines a YAML
semantic model with an LLM for natural-language questions. dbq's `catalog.yaml` and plan
compiler work the same way, in a small form.

- dbt Labs, MetricFlow. <https://pypi.org/project/metricflow/>
- Snowflake, Cortex Analyst and its semantic model specification.
  <https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst>

## Intermediate representations for text-to-SQL

dbq's model does not write SQL. It writes a JSON plan that deterministic code expands into
SQL. Research on cross-domain text-to-SQL uses the same idea under the name *intermediate
representation*:

- IRNet predicts SemQL, which is then converted to SQL.
  J. Guo et al., "Towards Complex Text-to-SQL in Cross-Domain Database with Intermediate
  Representation," ACL 2019. <https://aclanthology.org/P19-1444/>
- NatSQL drops clauses that are hard to infer from a question, such as JOIN ON and GROUP BY.
  Y. Gan et al., "Natural SQL: Making SQL Easier to Infer from Natural Language
  Specifications," Findings of EMNLP 2021. <https://aclanthology.org/2021.findings-emnlp.174>

dbq's plan goes further than these. Joins, the choice of fact table and aggregation are
outside the model's output entirely, because they come from the catalog. This makes the plan
closer to a semantic-layer query than to a simplified SQL.

## Constrained decoding

When the first plan does not compile, dbq generates again under a grammar that only admits
catalog names and valid plan syntax. Restricting decoding to valid output is established:

- PICARD rejects inadmissible tokens during decoding by parsing the SQL incrementally.
  T. Scholak, N. Schucher, D. Bahdanau, "PICARD: Parsing Incrementally for Constrained
  Auto-Regressive Decoding from Language Models," EMNLP 2021.
  <https://aclanthology.org/2021.emnlp-main.779>
- Synchromesh applies constrained semantic decoding to SQL and other languages.
  G. Poesia et al., "Synchromesh: Reliable Code Generation from Pre-trained Language
  Models," ICLR 2022. <https://arxiv.org/abs/2201.11227>
- dbq uses llama.cpp's GBNF grammars directly.
  <https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md>

## Linking questions to database values

dbq looks up which database values a question names and adds them to the prompt as a hint
line. This is *value linking* (also called anchor or entity retrieval):

- BRIDGE adds the cell values a question mentions, found by string matching, next to the
  matching columns. X. V. Lin, R. Socher, C. Xiong, "Bridging Textual and Tabular Data for
  Cross-Domain Text-to-SQL Semantic Parsing," Findings of EMNLP 2020.
  <https://arxiv.org/abs/2012.12627>
- CHESS retrieves values and catalog context before generating SQL on large databases.
  S. Talaei et al., "CHESS: Contextual Harnessing for Efficient SQL Synthesis," 2024.
  <https://arxiv.org/abs/2405.16755>
- BIRD is a benchmark that stresses database values and dirty data. J. Li et al., "Can LLM
  Already Serve as A Database Interface? A BIg Bench for Large-Scale Database Grounded
  Text-to-SQLs," NeurIPS 2023. <https://arxiv.org/abs/2305.03111>

## Synthetic training data

dbq trains only on questions generated from the catalog. The generation idea goes back to
grammar-generated canonical utterances for semantic parsers:

- Y. Wang, J. Berant, P. Liang, "Building a Semantic Parser Overnight," ACL 2015.
  <https://aclanthology.org/P15-1129/>
- Y. Zhao et al., "Importance of Synthesizing High-quality Data for Text-to-SQL Parsing,"
  Findings of ACL 2023, show that synthetic data helps only when it is realistic: no
  arbitrary joins, and typed, schema-aware sampling. <https://arxiv.org/abs/2212.08785>

dbq generates from the catalog, so every training plan uses valid joins by construction.

## Small open models for text-to-SQL

- H. Li et al., "CodeS: Towards Building Open-source Language Models for Text-to-SQL,"
  SIGMOD 2024, argue for open 1B–15B models over closed LLMs for privacy and cost.
  <https://arxiv.org/abs/2402.16347>
- dbq fine-tunes Qwen2.5-Coder-1.5B-Instruct (B. Hui et al., "Qwen2.5-Coder Technical
  Report," 2024, <https://arxiv.org/abs/2409.12186>) with LoRA (E. Hu et al., "LoRA:
  Low-Rank Adaptation of Large Language Models," ICLR 2022,
  <https://arxiv.org/abs/2106.09685>), using MLX for training and llama.cpp for inference.

Benchmarks: Spider (T. Yu et al., EMNLP 2018, <https://aclanthology.org/D18-1425/>) and
BIRD (above) are the standard ones. dbq is evaluated only on its own hand-written
AdventureWorks questions, so its numbers cannot be compared with Spider or BIRD scores.

## What is specific to dbq

We did not find a prior system that combines all of the following. The search was short,
so this is not a claim of novelty.

1. **A very small local model.** A 1.5B model runs on a laptop through llama.cpp. Most
   semantic-layer assistants instead give a large hosted LLM the semantic model in its
   prompt.
2. **No data in the model or the binary.** Training uses only made-up values. Real values
   reach the model only through a live, per-question, read-only lookup. Nothing is cached,
   and the binary embeds only the catalog's structure.
3. **Training data designed against shortcuts.** Hint lines are shuffled, so the correct
   dimension is not always listed first. Made-up values have the shape of the real column
   (codes for code columns). Several value filters per question are common. Each of these
   choices fixed a specific failure we measured.
4. **One catalog as the single source.** The compiler, the decoding grammar, the training
   data and the dbq binary are all generated from `catalog.yaml`. A new database therefore
   takes four steps: profile, review, regenerate and train.
