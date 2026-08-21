# Data licensing and attribution

The Apache-2.0 license in this repository applies to source code and project-authored
documentation. It does not relicense Wikipedia text, page metadata, or generated
document/token datasets.

Wikipedia text is generally available under Creative Commons Attribution-ShareAlike
terms, but imported or page-specific material can carry additional terms or exceptions.
The pipeline materially transforms the source by removing wiki markup and selected
non-prose content, normalizing Unicode/whitespace, filtering documents, assigning splits,
and optionally tokenizing and packing text.

Before redistributing generated artifacts, review at least:

- the [Wikimedia Terms of Use licensing section](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use/en#7._Licensing_of_Content);
- the source wiki's [copyrights page](https://simple.wikipedia.org/wiki/Wikipedia:Copyrights);
- Creative Commons' [CC BY-SA 4.0 deed and legal code](https://creativecommons.org/licenses/by-sa/4.0/);
- relevant page histories, footers, discussion pages, and imported-content notices where
  additional terms may apply.

A redistribution plan should retain the dump wiki/date/URL/checksums; distribute the
generated `DATA_CARD.md`; provide the applicable license notice and a clear change
notice; preserve page/revision IDs, titles, and URLs from `attribution.parquet`; provide
a reasonable path to author history; and address ShareAlike and downstream distribution
for both readable text and tokenized derivatives.

The manifest and attribution schema preserve inputs for such a strategy. They do not
prove that every source-page condition is satisfied, determine whether a particular
artifact is an adaptation or collection, or replace qualified legal review. The full
acceptance policy therefore requires separate human licensing review before a corpus is
published. This GitHub repository ships code and synthetic test fixtures, not a
Wikipedia corpus.
