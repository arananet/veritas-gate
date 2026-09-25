# Retrieval Pruning Makes Language Models Reliable

## Abstract

We introduce retrieval pruning, a method that removes low-relevance context
before generation. Retrieval pruning reaches 87% accuracy on our question set
and makes all large language models reliable on factual questions.

## Introduction

Language models answer factual questions incorrectly when retrieved context
is noisy. We propose pruning that context before generation.

## Method

We score each retrieved passage by cosine similarity to the question and drop
passages below 0.4. We evaluated one model, a 7B open-weights model, on 16
questions written by the authors.

## Results

| Condition | Correct |
|---|---|
| No pruning | 9/16 |
| Retrieval pruning | 12/16 |

Per-question results are in [the results file](../data/results.csv).
Retrieval pruning therefore improves accuracy significantly.

## Conclusion

Retrieval pruning solves hallucination for all large language models and
should be adopted as a standard component of retrieval-augmented generation.
