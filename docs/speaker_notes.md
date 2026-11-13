# Speaker notes: Client Intelligence Agent

## Slide 5: Client Intelligence Agent

Agent A is for a consultant or a sales lead. You type a company name and the service you want to sell. It researches the company from real, public sources, checks every claim it is about to make, and gives you a briefing, a proposal outline and a first email. It takes about three minutes, and the email is only sent after you approve it.

## Slide 6: A real run: Tata Consumer Products

This is a real run I did for this deck, not a mock-up. For Tata Consumer Products with the service line supply chain and distribution, the agent read 58 sources and kept 28 findings, each one backed by a quote that was checked against the source text. Then the critic reviewed the draft three times: it rejected claims in the first two rounds, and by round three all nine claims were supported. Total: 15 model calls and no cached answers.

## Slide 7: Six steps from name to briefing

Six steps. The planner splits the job into six research angles, including one built around the service you sell. Six researchers then work in parallel on real sources. Every finding must carry a quote that actually exists in its source, and invented ones are dropped. A writer drafts the briefing from verified findings only. A critic checks each claim against its evidence and sends weak ones back for another draft. Finally, you edit and approve the email, in amber because it is the human step.

## Slide 8: Real sources, labelled by strength

The agent only uses sources it can fetch and quote. Wikipedia, Wikidata and the company’s own site are strong evidence because they are full text. News is useful because it is current and dated, but a headline is thin evidence, so those items are labelled headline-only in the briefing. That honesty matters: a client can see at a glance how much weight each point can bear. You can also paste your own document, such as an annual report, and it becomes strong evidence.

## Slide 9: Why it cannot make things up

This is the slide that answers the obvious question: how do you stop an AI making things up? Three layers. First, code checks that every quote exists in its source and that every number in a claim appears in its evidence. Second, a critic model asks whether each claim is supported and whether it is relevant to the service being sold, and if it cannot judge, the claim is dropped. Third, the email is worded carefully. On the right is a real rejection from the demo run: the draft overstated a source, the critic caught it, and the claim was rewritten.

## Slide 10: What you get

Here is a real item from the Tata Consumer briefing. The hypothesis is that distributor channel friction was caused by stock dumping concerns. Its evidence is a dated headline, and it is clearly labelled headline-only. Under it, the agent proposes a workstream that fits the service line: distributor channel alignment and route-to-market optimization. On the right is the full package: the cited briefing, the proposal with scope and discovery questions, the outreach email for you to edit and approve, and files you can keep.

