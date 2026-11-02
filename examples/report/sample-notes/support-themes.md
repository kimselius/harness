# Support themes, Q3 2026

From tagging every ticket in July–September. 1,020 tickets total.

| theme | tickets | note |
| --- | --- | --- |
| export slow or stuck | 408 | peaks Tue–Thu mornings |
| login / SSO | 180 | mostly one customer's misconfigured IdP |
| billing questions | 141 | steady, no trend |
| data import errors | 96 | almost all malformed CSV from the same two tenants |
| everything else | 195 | |

Two things worth saying out loud:

The export complaints cluster in the morning, which lines up with the nightly job
still running when EU customers arrive. We have never correlated the two
timestamps properly — it is an impression from reading tickets, not a measurement.

Churn: 11 tenants left in Q3. Three of them mentioned export reliability in their
exit note. That is not enough to call export the cause of churn, and two of the
three were also over their contracted seat count and may have left on price.

Contradicts the ops review: the ops dashboard says 340 tickets/month, so ~1,020
for the quarter, which matches. But the ops review says 40% are export-related and
this tagging says 40% exactly (408/1,020). Fine, they agree.
