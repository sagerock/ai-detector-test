# Predictions, recorded before scanning

Written before any document was submitted. Published unedited so the analysis can be
checked against what was expected rather than against what turned up.

## Hypothesis

That formulaic professional registers read as machine-written to a stylometric
classifier. Law school explicitly teaches a structure: rule, illustration, application,
conclusion, numbered elements, parallel construction. If that structure is what a
detector keys on, then students are being trained toward the signature they'll be
punished for.

## Predictions

1. Most or all pre-2020 court opinions return "human." At the claimed error rate, 50
   documents cannot falsify that number and this does not attempt to.
2. Judicial prose scores **materially above** the general-prose baseline, because the
   register is formulaic by professional norm. The score distribution, not the verdict
   count, is the measurement that matters.
3. Encyclopedia biography, being condensed neutral summary with evaluative shorthand,
   scores **above** baseline for the same reason.
4. Rewrites produced by asking a model to sound less like AI score **higher** than the
   original, because models reaching for casual register overshoot into the tics they
   learned from other machine text.

## Decision rules

- Results get published whichever way they come out, including results that embarrass
  the hypothesis.
- A null result is reported as a null result, and the argument then rests on reasoning
  rather than on a number that failed to appear.
- Model pinned to `pangram-4`; the version returned by the API is recorded per run.
- Raw responses retained in full.

## Results

**Prediction 1 held.** 50/50 human.

**Prediction 2 was wrong.** Median max score 0.0000, 75th percentile 0.0001, highest in
sample 0.0035. Nothing above 0.05. Decade made no difference. Legal register is not
what the detector keys on.

**Prediction 3 was wrong.** 9/9 human, median 0.0001. Encyclopedia biography sits at
the same floor. Audie Murphy's article was chosen specifically because "most decorated
soldier of the Second World War" is that register at its purest, and it scored 0.0001.

**Prediction 4 was wrong.** All arms returned AI at fraction_ai = 1.00, scores 0.9585
to 0.9999. The rewrites scored marginally *lower* than the baseline, not higher, though
everything above 0.95 is saturated and those gaps are not meaningful. The humanizer
probe fired on none of them, including the seven where evading detection was the
literal instruction.

**Standing count: 1 of 4.** Recorded because a pre-registration that only ever confirms
its author isn't doing any work. Two proposed mechanisms were tested and both were
falsified. No mechanism is claimed.
