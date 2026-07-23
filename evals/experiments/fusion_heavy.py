"""Fusion past the optimum (800) — where the vector arm starts overriding lexical evidence."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("Fusion at 800, double the shipped weight. Pushes the paraphrase files "
              "higher still, but the keyword-shaped topic files give back recall as "
              "cross-arm agreement starts outvoting lexical evidence it should defer "
              "to. The upper bound on the knob: it marks where more trust in the "
              "vector arm stops being free.")
PARAMS = SearchParams(fusion_weight=800.0)
