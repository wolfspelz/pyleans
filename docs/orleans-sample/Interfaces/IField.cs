using Orleans;

namespace My.Interfaces;

[GenerateSerializer]
public class FieldProperties
{
    [Id(0)] public string Color { get; set; } = "black";
    [Id(1)] public int Count { get; set; } = 0;
    [Id(2)] public bool Selected { get; set; } = false;
}

public interface IField : IGrainWithStringKey
{
    Task<FieldProperties> Get();
    Task Select(bool value);
}
