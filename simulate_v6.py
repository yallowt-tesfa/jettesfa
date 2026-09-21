import random,tempfile,os
os.environ['JET_DEMO']='1'
import jet_app as j
j.DB=j.Path(tempfile.mkdtemp())/'sim.db';j.init_db(); j.historical_deal=lambda o:o.deal; random.seed(61)
N=20000; guard=0; ranges=0; decisions={}
for i in range(N):
 def o(good,comm):
  return j.Opportunity('sim',str(random.random()),'product','https://x',random.uniform(50,5000),'ILS','general','merchant',
   quality=random.uniform(.75,.99) if good else random.uniform(.1,.6), trust=random.uniform(.8,.99) if good else random.uniform(.1,.55),
   fit=random.uniform(.8,.99) if good else random.uniform(.15,.6),deal=random.uniform(.6,.95) if good else random.uniform(.1,.55),
   commission=comm,freshness=.9,price_confidence=random.uniform(.8,.99) if good else random.uniform(.1,.55),satisfaction=.85)
 a=o(False,1);b=o(True,0)
 if j.rank_truth(b,{}, {})>j.rank_truth(a,{},{}):guard+=1
 s=j.rank_truth(b,{},{});ranges+=0<=s<=100
 d=j.decision(b,{},{});decisions[d]=decisions.get(d,0)+1
print({'scenarios':N,'customer_value_beats_high_commission':guard,'score_in_range':ranges,'decisions':decisions})
