"""
Example user script — this is what a researcher would write.
Outputs must be saved to ./output/ so they get rsynced back.

This demo: generates a sine wave plot and saves a CSV.
Requires: matplotlib, pandas  (put in requirements.txt)
"""
import os, math
import pandas as pd
import matplotlib.pyplot as plt

os.makedirs('output', exist_ok=True)

# Simulate some analysis
x  = [i * 0.1 for i in range(200)]
y  = [math.sin(v) * math.exp(-v * 0.05) for v in x]

df = pd.DataFrame({'x': x, 'y': y})
df.to_csv('output/results.csv', index=False)

fig, ax = plt.subplots()
ax.plot(x, y)
ax.set_title('Damped sine wave')
fig.savefig('output/plot.png', dpi=150)

print(f'Done. {len(df)} rows written to output/results.csv')
