-- Delete all generate-marc workcoveragerecords records, so that new links will be created.
delete from workcoveragerecords where operation = 'generate-marc';

-- Delete any generate-marc timestamps records, so that the MARC work record coverage
-- provider will regenerate the MARC records as soon as possible.
delete from timestamps where service = 'MARC Record Work Coverage Provider (generate-marc)';
