public static class GrainIdExtensions
{
    public static string GetPrimaryKeyString(this GrainId grainId)
    {
        string? primaryKey = grainId.Key.ToString();
        if (primaryKey is null) {
            throw new Exception("GrainId doesn't contain a string key!");
        }
        return primaryKey;
    }
}