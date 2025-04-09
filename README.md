# django-from

Provides queryset encapsulation to get around Django's

    Cannot compute Aggregate('field'): 'field' is an aggregate

limitation. What Django currently lacks is queryset encapsulation, that
is, the ability to build a queryset upon an encapsulated queryset. Or,
in other words, put an existing queryset in the FROM clause of the SQL.

Django's Queryset cannot select from Subqueries and builds queries through
successive Joins only. And while Django supports Subqueries, it only supports 
them as annotations (in the SELECT clause) not as a source (in the FROM clause).

Because Django's QuerySest can be quite complicated this is a running
experiment. Tested on a particular use case and functional but quite
likely to break on some fancy query or other ...