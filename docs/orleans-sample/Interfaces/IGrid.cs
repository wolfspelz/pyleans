using Orleans;

namespace My.Interfaces;


public interface IGrid : IGrainWithStringKey
{
    Task<FieldProperties[][]> Get(int nx, int ny);
}
