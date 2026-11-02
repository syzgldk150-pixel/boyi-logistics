-- The arrive-list instance was installed with the display name "arrive-list";
-- the business calls it 到货清单. Only that exact production instance under
-- its original name is renamed, so rerunning or a renamed instance is a no-op.
UPDATE automation_projects
SET display_name = '到货清单'
WHERE automation_id = '9ed48d33-54dc-4d96-ba5c-28fb46d6f721'
  AND plugin_id = 'sync_arrive_list_v2'
  AND display_name = 'arrive-list';
